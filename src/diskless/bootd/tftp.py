"""TFTP 프로토콜. DESIGN.md 3.2 — RRQ만 허용, 블록 크기 협상(RFC 2348).

iPXE 바이너리는 정적 파일 그대로, 클라이언트별 sanboot 스크립트는
요청 시점에 ClientBinding을 조회해 동적 생성한다(3.3) — 이 조회/오케스트레이션
호출이 이 프로세스 안에서 바로 이루어진다(별도 프로세스로 안 쪼갬).

패킷 유실에 대비해 마지막으로 보낸 패킷(OACK/DATA)마다 재전송 타이머를 걸고,
ACK이 오면 취소한다. `MAX_RETRANSMITS`번 재전송해도 ACK이 없으면 전송을 포기한다
(클라이언트가 이미 가버렸다고 판단). `clock`은 테스트에서 실시간 대기 없이 시간을
흘려보내기 위한 주입 지점(기본은 reactor).

한계(현재 범위): 전통적인 TFTP처럼 전송마다 새 임시 포트를 쓰지 않고 이 포트(69)
하나에서 클라이언트 addr로 세션을 구분한다 — NAT 없는 동일 LAN의 PXE 부팅
시나리오라 문제되지 않는다.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from twisted.internet import protocol, reactor
from twisted.internet.threads import deferToThread
from twisted.logger import Logger

from diskless.config import Config

OPCODE_RRQ = 1
OPCODE_WRQ = 2
OPCODE_DATA = 3
OPCODE_ACK = 4
OPCODE_ERROR = 5
OPCODE_OACK = 6

ERR_ACCESS_VIOLATION = 2
ERR_ILLEGAL_OPERATION = 4
STATIC_EXTENSIONS = (".kpxe", ".efi")

DEFAULT_BLOCK_SIZE = 512
MAX_BLOCK_SIZE = 1468  # 일반적인 이더넷 MTU 기준 안전값(RFC 2348 상한은 65464)

RETRANSMIT_INTERVAL_S = 2.0
MAX_RETRANSMITS = 5

log = Logger()


def parse_rrq(payload: bytes) -> tuple[str, dict[str, str]]:
    fields = payload.split(b"\x00")
    filename = fields[0].decode()
    options = dict(zip((f.decode() for f in fields[2:-1:2]), (f.decode() for f in fields[3:-1:2])))
    return filename, options


def error_packet(code: int, message: str) -> bytes:
    return OPCODE_ERROR.to_bytes(2, "big") + code.to_bytes(2, "big") + message.encode() + b"\x00"


def data_packet(block: int, chunk: bytes) -> bytes:
    return OPCODE_DATA.to_bytes(2, "big") + block.to_bytes(2, "big") + chunk


def oack_packet(options: dict[str, str]) -> bytes:
    body = b"".join(k.encode() + b"\x00" + v.encode() + b"\x00" for k, v in options.items())
    return OPCODE_OACK.to_bytes(2, "big") + body


def negotiate_block_size(requested: dict[str, str]) -> int:
    if "blksize" not in requested:
        return DEFAULT_BLOCK_SIZE
    return max(8, min(int(requested["blksize"]), MAX_BLOCK_SIZE))


class _Transfer:
    """RRQ 하나(파일 하나)의 전송 상태.

    `pending_block`은 "우리가 방금 보내서 클라이언트의 ACK을 기다리는 블록 번호"다.
    OACK을 보낸 직후에는 0(RFC 2348 관례). 전송 종료는 "이미 block_size보다 짧은
    블록을 보냈고, 그 블록의 ACK을 받은 시점"이다 — 그 시점엔 더 보낼 게 없으므로
    handle_ack이 (None, True)를 반환한다.
    """

    def __init__(self, payload: bytes, block_size: int, use_oack: bool) -> None:
        self.payload = payload
        self.block_size = block_size
        self.pending_block = 0 if use_oack else 1
        self._sent_chunks: dict[int, bytes] = {}

    def _chunk(self, block: int) -> bytes:
        start = (block - 1) * self.block_size
        return self.payload[start : start + self.block_size]

    def first_packet(self) -> bytes:
        if self.pending_block == 0:
            return oack_packet({"blksize": str(self.block_size)})
        chunk = self._chunk(1)
        self._sent_chunks[1] = chunk
        return data_packet(1, chunk)

    def handle_ack(self, block: int) -> tuple[bytes | None, bool]:
        """반환: (다음에 보낼 패킷 또는 None, 전송 종료 여부)."""
        if block != self.pending_block:
            return None, False  # 중복/순서가 어긋난 ACK — 재전송 타이머 없이 그냥 무시(범위 밖)

        if block != 0 and len(self._sent_chunks[block]) < self.block_size:
            return None, True  # 방금 보낸 게 이미 마지막(짧은) 블록이었음 — 더 보낼 것 없음

        next_block = block + 1
        chunk = self._chunk(next_block)
        self._sent_chunks[next_block] = chunk
        self.pending_block = next_block
        return data_packet(next_block, chunk), False


class TftpProtocol(protocol.DatagramProtocol):
    def __init__(self, cfg: Config, tftp_root: Path, conn: sqlite3.Connection, clock=None) -> None:
        self.cfg = cfg
        self.tftp_root = tftp_root
        self.conn = conn
        self.clock = clock or reactor
        self._transfers: dict[tuple[str, int], _Transfer] = {}
        self._retransmit_calls: dict[tuple[str, int], object] = {}

    def datagramReceived(self, data: bytes, addr: tuple[str, int]) -> None:
        opcode = int.from_bytes(data[:2], "big")

        if opcode == OPCODE_WRQ:
            # 인증 없는 프로토콜이라 쓰기를 열면 서버 파일을 누구나 덮어쓸 수 있음(3.2)
            self.transport.write(error_packet(ERR_ACCESS_VIOLATION, "write not allowed"), addr)
            return
        if opcode == OPCODE_RRQ:
            filename, options = parse_rrq(data[2:])
            d = deferToThread(self._resolve_payload, filename)
            d.addCallback(self._start_transfer, addr, options)
            d.addErrback(lambda f: log.failure("TFTP RRQ 처리 실패: {filename}", failure=f, filename=filename))
            return
        if opcode == OPCODE_ACK:
            self._handle_ack(int.from_bytes(data[2:4], "big"), addr)
            return
        # ERROR 등 나머지 opcode는 세션이 있다면 정리만 하고 무시
        self._transfers.pop(addr, None)

    def _resolve_payload(self, filename: str) -> bytes:
        if filename.endswith(STATIC_EXTENSIONS):
            return (self.tftp_root / filename).read_bytes()

        # 동적 sanboot 스크립트: 파일명 규약 "boot/<mac>.ipxe"에서 MAC을 뽑아낸다.
        # (실제 규약은 iPXE 부트스크립트가 어떤 URL로 재요청하는지에 맞춰 조정)
        from diskless.orchestration.session import build_sanboot_script

        client_mac = Path(filename).stem
        return build_sanboot_script(self.cfg, self.conn, client_mac)

    def _start_transfer(self, payload: bytes, addr: tuple[str, int], options: dict[str, str]) -> None:
        block_size = negotiate_block_size(options)
        transfer = _Transfer(payload, block_size, use_oack=bool(options))
        self._transfers[addr] = transfer
        self._send_and_arm_retransmit(addr, transfer.first_packet())

    def _handle_ack(self, block: int, addr: tuple[str, int]) -> None:
        transfer = self._transfers.get(addr)
        if transfer is None:
            return
        self._cancel_retransmit(addr)  # ACK 받았으니 직전에 보낸 패킷의 재전송 타이머는 취소
        packet, done = transfer.handle_ack(block)
        if packet is not None:
            self._send_and_arm_retransmit(addr, packet)
        if done:
            self._transfers.pop(addr, None)

    def _send_and_arm_retransmit(self, addr: tuple[str, int], packet: bytes, attempt: int = 0) -> None:
        self.transport.write(packet, addr)
        self._retransmit_calls[addr] = self.clock.callLater(
            RETRANSMIT_INTERVAL_S, self._on_retransmit_timeout, addr, packet, attempt
        )

    def _on_retransmit_timeout(self, addr: tuple[str, int], packet: bytes, attempt: int) -> None:
        self._retransmit_calls.pop(addr, None)
        if attempt + 1 >= MAX_RETRANSMITS:
            log.warn("TFTP 재전송 한도 초과, 전송 포기: {addr}", addr=addr)
            self._transfers.pop(addr, None)
            return
        self._send_and_arm_retransmit(addr, packet, attempt + 1)

    def _cancel_retransmit(self, addr: tuple[str, int]) -> None:
        call = self._retransmit_calls.pop(addr, None)
        if call is not None and call.active():
            call.cancel()
