from pathlib import Path

import pytest
from twisted.internet import defer
from twisted.internet.task import Clock

import diskless.bootd.tftp as tftp_mod
from diskless.bootd.tftp import TftpProtocol
from tests.bootd.fake_transport import FakeDatagramTransport

ADDR = ("198.51.100.5", 55123)


def _run_sync_deferToThread(monkeypatch):
    """deferToThread를 동기 실행으로 바꿔서 reactor 없이도 콜백 체인이 즉시 끝나게 한다."""
    monkeypatch.setattr(tftp_mod, "deferToThread", lambda f, *a, **kw: defer.execute(f, *a, **kw))


def make_protocol(tmp_path: Path, monkeypatch) -> tuple[TftpProtocol, FakeDatagramTransport, Clock]:
    _run_sync_deferToThread(monkeypatch)
    clock = Clock()
    proto = TftpProtocol(cfg=None, tftp_root=tmp_path, conn=None, clock=clock)
    transport = FakeDatagramTransport()
    proto.transport = transport
    return proto, transport, clock


def build_rrq(filename: str, options: dict[str, str] | None = None) -> bytes:
    payload = filename.encode() + b"\x00octet\x00"
    for key, value in (options or {}).items():
        payload += key.encode() + b"\x00" + value.encode() + b"\x00"
    return (1).to_bytes(2, "big") + payload


def build_ack(block: int) -> bytes:
    return (4).to_bytes(2, "big") + block.to_bytes(2, "big")


def parse_data(packet: bytes) -> tuple[int, int, bytes]:
    opcode = int.from_bytes(packet[:2], "big")
    block = int.from_bytes(packet[2:4], "big")
    return opcode, block, packet[4:]


def test_wrq_is_rejected(tmp_path, monkeypatch):
    proto, transport, _clock = make_protocol(tmp_path, monkeypatch)
    wrq = (2).to_bytes(2, "big") + b"evil.kpxe\x00octet\x00"
    proto.datagramReceived(wrq, ADDR)

    assert len(transport.sent) == 1
    reply, addr = transport.sent[0]
    assert addr == ADDR
    assert int.from_bytes(reply[:2], "big") == tftp_mod.OPCODE_ERROR


def test_rrq_without_options_sends_first_data_block(tmp_path, monkeypatch):
    (tmp_path / "undionly.kpxe").write_bytes(b"x" * 100)
    proto, transport, _clock = make_protocol(tmp_path, monkeypatch)

    proto.datagramReceived(build_rrq("undionly.kpxe"), ADDR)

    assert len(transport.sent) == 1
    reply, addr = transport.sent[0]
    opcode, block, chunk = parse_data(reply)
    assert addr == ADDR
    assert opcode == tftp_mod.OPCODE_DATA
    assert block == 1
    assert chunk == b"x" * 100


def test_rrq_with_blksize_option_sends_oack(tmp_path, monkeypatch):
    (tmp_path / "undionly.kpxe").write_bytes(b"y" * 3000)
    proto, transport, _clock = make_protocol(tmp_path, monkeypatch)

    proto.datagramReceived(build_rrq("undionly.kpxe", {"blksize": "1024"}), ADDR)

    assert len(transport.sent) == 1
    reply, _addr = transport.sent[0]
    assert int.from_bytes(reply[:2], "big") == tftp_mod.OPCODE_OACK
    assert b"blksize\x001024\x00" in reply


def test_ack_zero_after_oack_sends_first_negotiated_block(tmp_path, monkeypatch):
    (tmp_path / "undionly.kpxe").write_bytes(b"a" * 3000)
    proto, transport, _clock = make_protocol(tmp_path, monkeypatch)
    proto.datagramReceived(build_rrq("undionly.kpxe", {"blksize": "1024"}), ADDR)
    transport.sent.clear()

    proto.datagramReceived(build_ack(0), ADDR)

    assert len(transport.sent) == 1
    reply, _addr = transport.sent[0]
    opcode, block, chunk = parse_data(reply)
    assert opcode == tftp_mod.OPCODE_DATA
    assert block == 1
    assert chunk == b"a" * 1024


def test_ack_for_data_block_sends_next_block(tmp_path, monkeypatch):
    (tmp_path / "undionly.kpxe").write_bytes(b"b" * 1200)  # 512바이트 기준 3블록(512/512/176)
    proto, transport, _clock = make_protocol(tmp_path, monkeypatch)
    proto.datagramReceived(build_rrq("undionly.kpxe"), ADDR)
    transport.sent.clear()

    proto.datagramReceived(build_ack(1), ADDR)

    reply, _addr = transport.sent[0]
    opcode, block, chunk = parse_data(reply)
    assert block == 2
    assert len(chunk) == 512


def test_short_final_block_ends_transfer(tmp_path, monkeypatch):
    (tmp_path / "undionly.kpxe").write_bytes(b"c" * 100)  # 512바이트 미만 -> 블록 1개로 끝
    proto, transport, _clock = make_protocol(tmp_path, monkeypatch)
    proto.datagramReceived(build_rrq("undionly.kpxe"), ADDR)
    transport.sent.clear()

    proto.datagramReceived(build_ack(1), ADDR)

    # 마지막 블록까지 ACK 받았으니 더 보낼 게 없어야 함
    assert transport.sent == []
    assert ADDR not in proto._transfers


def test_ack_for_unknown_transfer_is_ignored(tmp_path, monkeypatch):
    proto, transport, _clock = make_protocol(tmp_path, monkeypatch)
    proto.datagramReceived(build_ack(1), ("203.0.113.9", 4000))
    assert transport.sent == []


# ---- 재전송 타이머 ----


def test_packet_retransmitted_if_ack_not_received_in_time(tmp_path, monkeypatch):
    (tmp_path / "undionly.kpxe").write_bytes(b"x" * 100)
    proto, transport, clock = make_protocol(tmp_path, monkeypatch)
    proto.datagramReceived(build_rrq("undionly.kpxe"), ADDR)
    assert len(transport.sent) == 1
    first_packet, _addr = transport.sent[0]

    clock.advance(tftp_mod.RETRANSMIT_INTERVAL_S)

    assert len(transport.sent) == 2
    second_packet, addr = transport.sent[1]
    assert second_packet == first_packet
    assert addr == ADDR


def test_ack_cancels_pending_retransmit(tmp_path, monkeypatch):
    (tmp_path / "undionly.kpxe").write_bytes(b"c" * 100)  # 100바이트 -> 블록 1개로 끝
    proto, transport, clock = make_protocol(tmp_path, monkeypatch)
    proto.datagramReceived(build_rrq("undionly.kpxe"), ADDR)

    proto.datagramReceived(build_ack(1), ADDR)  # 마지막 블록 ack -> 전송 종료
    sent_count_after_ack = len(transport.sent)

    clock.advance(tftp_mod.RETRANSMIT_INTERVAL_S * 2)  # 재전송 타이머가 안 살아있어야 함

    assert len(transport.sent) == sent_count_after_ack


def test_transfer_abandoned_after_max_retransmits(tmp_path, monkeypatch):
    (tmp_path / "undionly.kpxe").write_bytes(b"x" * 100)
    proto, transport, clock = make_protocol(tmp_path, monkeypatch)
    proto.datagramReceived(build_rrq("undionly.kpxe"), ADDR)

    for _ in range(tftp_mod.MAX_RETRANSMITS):
        clock.advance(tftp_mod.RETRANSMIT_INTERVAL_S)

    assert ADDR not in proto._transfers
    sent_count = len(transport.sent)

    clock.advance(tftp_mod.RETRANSMIT_INTERVAL_S)  # 포기한 뒤로는 더 안 보내야 함
    assert len(transport.sent) == sent_count
