from diskless.bootd.dhcp_standalone import LeasePool, StandaloneDhcpProtocol
from diskless.dhcp import common
from tests.bootd.fake_transport import FakeDatagramTransport
from tests.dhcp.dhcp_fixtures import build_request_packet

SERVER_IP = "192.0.2.1"
CLIENT_ADDR = ("255.255.255.255", 68)


def make_protocol() -> tuple[StandaloneDhcpProtocol, FakeDatagramTransport, LeasePool]:
    pool = LeasePool("192.0.2.0/24")
    proto = StandaloneDhcpProtocol(server_ip=SERVER_IP, boot_filename="undionly.kpxe", lease_pool=pool)
    transport = FakeDatagramTransport()
    proto.transport = transport
    return proto, transport, pool


def test_discover_triggers_offer_with_allocated_ip():
    proto, transport, pool = make_protocol()
    packet = build_request_packet(mac="aa:bb:cc:dd:ee:ff", message_type=common.MSG_DISCOVER)

    proto.datagramReceived(packet, CLIENT_ADDR)

    assert len(transport.sent) == 1
    reply, _addr = transport.sent[0]
    parsed = common.parse_request(reply)
    assert parsed.message_type == common.MSG_OFFER
    assert reply[16:20] == bytes(int(p) for p in pool.offer("aa:bb:cc:dd:ee:ff").split("."))


def test_discover_from_pxe_client_includes_pxe_options():
    proto, transport, _pool = make_protocol()
    packet = build_request_packet(mac="aa:bb:cc:dd:ee:ff", vendor_class=b"PXEClient", message_type=common.MSG_DISCOVER)

    proto.datagramReceived(packet, CLIENT_ADDR)

    reply, _addr = transport.sent[0]
    options = reply[240:]
    assert bytes([common.OPT_VENDOR_CLASS, len(common.PXE_VENDOR_CLASS)]) + common.PXE_VENDOR_CLASS in options


def test_discover_from_non_pxe_client_omits_pxe_options():
    proto, transport, _pool = make_protocol()
    packet = build_request_packet(mac="aa:bb:cc:dd:ee:ff", vendor_class=None, message_type=common.MSG_DISCOVER)

    proto.datagramReceived(packet, CLIENT_ADDR)

    reply, _addr = transport.sent[0]
    options = reply[240:]
    assert bytes([common.OPT_VENDOR_CLASS]) not in options or common.PXE_VENDOR_CLASS not in options


def test_request_triggers_ack_with_committed_ip():
    proto, transport, pool = make_protocol()
    discover = build_request_packet(mac="aa:bb:cc:dd:ee:ff", message_type=common.MSG_DISCOVER)
    proto.datagramReceived(discover, CLIENT_ADDR)
    offered_ip = pool.offer("aa:bb:cc:dd:ee:ff")

    request = build_request_packet(
        mac="aa:bb:cc:dd:ee:ff",
        message_type=common.MSG_REQUEST,
        requested_ip=bytes(int(p) for p in offered_ip.split(".")),
    )
    proto.datagramReceived(request, CLIENT_ADDR)

    assert len(transport.sent) == 2
    reply, _addr = transport.sent[1]
    parsed = common.parse_request(reply)
    assert parsed.message_type == common.MSG_ACK
    assert reply[16:20] == bytes(int(p) for p in offered_ip.split("."))


def test_unrelated_message_type_is_ignored():
    proto, transport, _pool = make_protocol()
    release_like_packet = build_request_packet(mac="aa:bb:cc:dd:ee:ff", message_type=7)  # RELEASE(7) 미지원
    proto.datagramReceived(release_like_packet, CLIENT_ADDR)
    assert transport.sent == []


def test_reply_sent_to_relay_when_giaddr_present():
    proto, transport, _pool = make_protocol()
    packet = build_request_packet(
        mac="aa:bb:cc:dd:ee:ff", message_type=common.MSG_DISCOVER, giaddr=bytes([203, 0, 113, 1])
    )

    proto.datagramReceived(packet, ("198.51.100.5", 67))

    _reply, addr = transport.sent[0]
    assert addr == ("203.0.113.1", 67)


def test_reply_broadcasts_when_flag_set_and_not_relayed():
    proto, transport, _pool = make_protocol()
    packet = build_request_packet(mac="aa:bb:cc:dd:ee:ff", message_type=common.MSG_DISCOVER, broadcast=True)

    proto.datagramReceived(packet, ("0.0.0.0", 68))

    _reply, addr = transport.sent[0]
    assert addr == ("255.255.255.255", 68)


def test_reply_falls_back_to_sender_addr_otherwise():
    proto, transport, _pool = make_protocol()
    packet = build_request_packet(mac="aa:bb:cc:dd:ee:ff", message_type=common.MSG_DISCOVER)

    proto.datagramReceived(packet, ("198.51.100.5", 68))

    _reply, addr = transport.sent[0]
    assert addr == ("198.51.100.5", 68)
