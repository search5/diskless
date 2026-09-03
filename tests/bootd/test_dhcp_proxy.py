from diskless.bootd.dhcp_proxy import ProxyDhcpProtocol
from diskless.dhcp import common
from tests.bootd.fake_transport import FakeDatagramTransport
from tests.dhcp.dhcp_fixtures import build_request_packet


def make_protocol() -> tuple[ProxyDhcpProtocol, FakeDatagramTransport]:
    proto = ProxyDhcpProtocol(server_ip="192.0.2.1", boot_filename="undionly.kpxe")
    transport = FakeDatagramTransport()
    proto.transport = transport
    return proto, transport


def test_ignores_non_pxe_client():
    proto, transport = make_protocol()
    packet = build_request_packet(vendor_class=None)
    proto.datagramReceived(packet, ("198.51.100.5", 68))
    assert transport.sent == []


def test_responds_to_pxe_discover_without_assigning_ip():
    proto, transport = make_protocol()
    packet = build_request_packet(mac="aa:bb:cc:dd:ee:ff", xid=0x42, vendor_class=b"PXEClient")
    proto.datagramReceived(packet, ("198.51.100.5", 4011))

    assert len(transport.sent) == 1
    reply, addr = transport.sent[0]
    assert addr == ("198.51.100.5", 4011)

    parsed = common.parse_request(reply)
    assert parsed.message_type == common.MSG_OFFER
    assert parsed.xid == (0x42).to_bytes(4, "big")
    assert reply[16:20] == b"\x00\x00\x00\x00"  # yiaddr = 0, Proxy DHCP는 IP를 할당하지 않음

    options = reply[240:]
    assert bytes([common.OPT_VENDOR_CLASS, len(common.PXE_VENDOR_CLASS)]) + common.PXE_VENDOR_CLASS in options
    assert bytes([common.OPT_BOOTFILE_NAME, len(b"undionly.kpxe\x00")]) + b"undionly.kpxe\x00" in options


def test_reply_sent_to_relay_when_giaddr_present():
    proto, transport = make_protocol()
    packet = build_request_packet(vendor_class=b"PXEClient", giaddr=bytes([203, 0, 113, 1]))

    proto.datagramReceived(packet, ("198.51.100.5", 4011))

    _reply, addr = transport.sent[0]
    assert addr == ("203.0.113.1", 67)


def test_reply_broadcasts_when_flag_set_and_not_relayed():
    proto, transport = make_protocol()
    packet = build_request_packet(vendor_class=b"PXEClient", broadcast=True)

    proto.datagramReceived(packet, ("0.0.0.0", 68))

    _reply, addr = transport.sent[0]
    assert addr == ("255.255.255.255", 68)
