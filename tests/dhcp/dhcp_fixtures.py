"""테스트용 DHCP 패킷 조립기. 실제 클라이언트가 보내는 패킷을 흉내내되,
`diskless.dhcp.common`의 구현과는 독립적으로 바이트를 직접 조립한다
(구현을 그대로 베껴서 자기 자신과 비교하는 함정을 피하기 위함).
"""

from __future__ import annotations

MAGIC_COOKIE = bytes([99, 130, 83, 99])


def mac_to_bytes(mac: str) -> bytes:
    return bytes(int(part, 16) for part in mac.split(":"))


def build_request_packet(
    *,
    mac: str = "aa:bb:cc:dd:ee:ff",
    xid: int = 0x12345678,
    message_type: int = 1,  # DHCPDISCOVER
    vendor_class: bytes | None = b"PXEClient",
    requested_ip: bytes | None = None,
    options_extra: bytes = b"",
    giaddr: bytes = b"\x00\x00\x00\x00",
    broadcast: bool = False,
) -> bytes:
    chaddr = mac_to_bytes(mac).ljust(16, b"\x00")
    flags = b"\x80\x00" if broadcast else b"\x00\x00"  # RFC 2131: 최상위 비트 = broadcast
    header = (
        bytes([1])  # op = BOOTREQUEST
        + bytes([1])  # htype = Ethernet
        + bytes([6])  # hlen
        + bytes([0])  # hops
        + xid.to_bytes(4, "big")
        + b"\x00\x00"  # secs
        + flags
        + b"\x00\x00\x00\x00"  # ciaddr
        + b"\x00\x00\x00\x00"  # yiaddr
        + b"\x00\x00\x00\x00"  # siaddr
        + giaddr
        + chaddr
        + b"\x00" * 64  # sname
        + b"\x00" * 128  # file
    )
    options = bytes([53, 1, message_type])
    if vendor_class is not None:
        options += bytes([60, len(vendor_class)]) + vendor_class
    if requested_ip is not None:
        options += bytes([50, 4]) + requested_ip
    options += options_extra
    options += bytes([255])
    return header + MAGIC_COOKIE + options
