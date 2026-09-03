from diskless.dhcp import common
from tests.dhcp.dhcp_fixtures import build_request_packet, mac_to_bytes


def test_parse_request_extracts_mac_and_vendor_class():
    packet = build_request_packet(mac="aa:bb:cc:dd:ee:ff", vendor_class=b"PXEClient")
    req = common.parse_request(packet)
    assert req.client_mac == "aa:bb:cc:dd:ee:ff"
    assert req.vendor_class == b"PXEClient"
    assert common.is_pxe_client(req)


def test_parse_request_non_pxe_client():
    packet = build_request_packet(vendor_class=None)
    req = common.parse_request(packet)
    assert req.vendor_class is None
    assert not common.is_pxe_client(req)


def test_parse_request_echoes_xid_and_chaddr():
    packet = build_request_packet(mac="11:22:33:44:55:66", xid=0xDEADBEEF)
    req = common.parse_request(packet)
    assert req.xid == (0xDEADBEEF).to_bytes(4, "big")
    assert req.chaddr[:6] == mac_to_bytes("11:22:33:44:55:66")


def test_parse_request_message_type():
    discover = build_request_packet(message_type=1)
    request = build_request_packet(message_type=3)
    assert common.parse_request(discover).message_type == common.MSG_DISCOVER
    assert common.parse_request(request).message_type == common.MSG_REQUEST


def test_parse_request_reads_requested_ip_option():
    packet = build_request_packet(message_type=3, requested_ip=bytes([10, 0, 0, 42]))
    req = common.parse_request(packet)
    assert req.requested_ip == bytes([10, 0, 0, 42])


def test_parse_request_without_requested_ip_option():
    packet = build_request_packet(message_type=1, requested_ip=None)
    assert common.parse_request(packet).requested_ip is None


def test_parse_request_default_giaddr_is_zero_and_not_broadcast():
    packet = build_request_packet()
    req = common.parse_request(packet)
    assert req.giaddr == common.ZERO_IP
    assert req.broadcast is False


def test_parse_request_reads_giaddr_when_relayed():
    packet = build_request_packet(giaddr=bytes([203, 0, 113, 1]))
    req = common.parse_request(packet)
    assert req.giaddr == bytes([203, 0, 113, 1])


def test_parse_request_reads_broadcast_flag():
    packet = build_request_packet(broadcast=True)
    assert common.parse_request(packet).broadcast is True


def test_bytes_to_ip():
    assert common.bytes_to_ip(bytes([192, 0, 2, 1])) == "192.0.2.1"


def test_reply_destination_prefers_giaddr_when_relayed():
    req = common.parse_request(build_request_packet(giaddr=bytes([203, 0, 113, 1]), broadcast=True))
    assert common.reply_destination(req, ("198.51.100.5", 68)) == ("203.0.113.1", 67)


def test_reply_destination_broadcasts_when_flag_set_and_not_relayed():
    req = common.parse_request(build_request_packet(broadcast=True))
    assert common.reply_destination(req, ("198.51.100.5", 68)) == ("255.255.255.255", 68)


def test_reply_destination_falls_back_to_sender_addr():
    req = common.parse_request(build_request_packet())
    assert common.reply_destination(req, ("198.51.100.5", 68)) == ("198.51.100.5", 68)


def test_build_pxe_options_contains_server_and_filename():
    opts = common.build_pxe_options("192.0.2.1", "undionly.kpxe")
    assert opts == bytes([66, 9]) + b"192.0.2.1" + bytes([67, 14]) + b"undionly.kpxe\x00"


def test_build_reply_echoes_xid_and_chaddr_and_sets_message_type():
    req_packet = build_request_packet(mac="aa:bb:cc:dd:ee:ff", xid=0x1)
    req = common.parse_request(req_packet)

    reply = common.build_reply(
        message_type=common.MSG_OFFER,
        xid=req.xid,
        chaddr=req.chaddr,
        yiaddr=bytes([10, 0, 0, 5]),
        siaddr=bytes([10, 0, 0, 1]),
        server_id=bytes([10, 0, 0, 1]),
        boot_filename="undionly.kpxe",
    )

    assert reply[4:8] == req.xid
    assert reply[28:34] == req.chaddr[:6]
    assert reply[16:20] == bytes([10, 0, 0, 5])  # yiaddr
    assert reply[20:24] == bytes([10, 0, 0, 1])  # siaddr

    parsed_back = common.parse_request(reply)
    assert parsed_back.message_type == common.MSG_OFFER


def test_build_reply_includes_lease_time_and_pxe_options():
    reply = common.build_reply(
        message_type=common.MSG_ACK,
        xid=b"\x00\x00\x00\x01",
        chaddr=b"\xaa" * 16,
        yiaddr=bytes([10, 0, 0, 5]),
        siaddr=bytes([10, 0, 0, 1]),
        server_id=bytes([10, 0, 0, 1]),
        boot_filename="undionly.kpxe",
        lease_seconds=3600,
        subnet_mask=bytes([255, 255, 255, 0]),
        pxe=True,
    )
    # 옵션 영역에 lease(51)/mask(1)/vendor-class(60)/tftp(66)/file(67)가 모두 들어있는지 확인
    options = reply[240:]
    assert bytes([51, 4]) + (3600).to_bytes(4, "big") in options
    assert bytes([1, 4]) + bytes([255, 255, 255, 0]) in options
    assert bytes([60, len(common.PXE_VENDOR_CLASS)]) + common.PXE_VENDOR_CLASS in options


def test_build_reply_echoes_giaddr_for_relay():
    reply = common.build_reply(
        message_type=common.MSG_OFFER,
        xid=b"\x00\x00\x00\x01",
        chaddr=b"\xaa" * 16,
        yiaddr=bytes([10, 0, 0, 5]),
        siaddr=bytes([10, 0, 0, 1]),
        server_id=bytes([10, 0, 0, 1]),
        boot_filename="undionly.kpxe",
        giaddr=bytes([203, 0, 113, 1]),
    )
    assert reply[24:28] == bytes([203, 0, 113, 1])
