"""Standalone/Proxy DHCP가 공유하는 패킷 파싱/조립 로직. DESIGN.md 3.1.

두 모드의 차이는 "IP 할당 로직을 포함하느냐(Standalone) / PXE 옵션 응답만
하느냐(Proxy)" 뿐이며, 패킷 파싱/조립은 여기 하나로 모은다.
"""

from __future__ import annotations

from dataclasses import dataclass

OPT_SUBNET_MASK = 1
OPT_ROUTER = 3
OPT_REQUESTED_IP = 50
OPT_LEASE_TIME = 51
OPT_MESSAGE_TYPE = 53
OPT_SERVER_ID = 54
OPT_VENDOR_CLASS = 60
OPT_TFTP_SERVER_NAME = 66
OPT_BOOTFILE_NAME = 67
OPT_END = 255

MSG_DISCOVER = 1
MSG_OFFER = 2
MSG_REQUEST = 3
MSG_ACK = 5
MSG_NAK = 6

PXE_VENDOR_CLASS = b"PXEClient"
MAGIC_COOKIE = bytes([99, 130, 83, 99])
ZERO_IP = b"\x00\x00\x00\x00"
BROADCAST_FLAG_MASK = 0x8000  # RFC 2131 flags 필드 최상위 비트

_BOOTP_HEADER_LEN = 236  # op..file 고정 필드
_OPTIONS_START = _BOOTP_HEADER_LEN + len(MAGIC_COOKIE)


@dataclass(frozen=True)
class DhcpRequest:
    message_type: int | None
    xid: bytes  # 4바이트, 응답에 그대로 echo
    chaddr: bytes  # 16바이트, 응답에 그대로 echo
    client_mac: str
    vendor_class: bytes | None
    requested_ip: bytes | None  # 옵션 50 (DHCPREQUEST에서만 보통 존재)
    giaddr: bytes  # 4바이트. 0이 아니면 릴레이 경유(RFC 2131 4.1) — 응답은 여기로 유니캐스트
    broadcast: bool  # flags 필드 최상위 비트. 클라이언트가 아직 IP가 없어 브로드캐스트 응답을 요청


def _parse_raw_options(packet: bytes) -> dict[int, bytes]:
    options: dict[int, bytes] = {}
    i = _OPTIONS_START
    while i < len(packet) and packet[i] != OPT_END:
        if packet[i] == 0:  # PAD
            i += 1
            continue
        opt, length = packet[i], packet[i + 1]
        options[opt] = packet[i + 2 : i + 2 + length]
        i += 2 + length
    return options


def parse_request(packet: bytes) -> DhcpRequest:
    flags = int.from_bytes(packet[10:12], "big")
    giaddr = packet[24:28]
    xid = packet[4:8]
    chaddr = packet[28:44]
    client_mac = ":".join(f"{b:02x}" for b in chaddr[:6])

    options = _parse_raw_options(packet)
    message_type = options[OPT_MESSAGE_TYPE][0] if OPT_MESSAGE_TYPE in options else None

    return DhcpRequest(
        message_type=message_type,
        xid=xid,
        chaddr=chaddr,
        client_mac=client_mac,
        vendor_class=options.get(OPT_VENDOR_CLASS),
        requested_ip=options.get(OPT_REQUESTED_IP),
        giaddr=giaddr,
        broadcast=bool(flags & BROADCAST_FLAG_MASK),
    )


def is_pxe_client(request: DhcpRequest) -> bool:
    return request.vendor_class == PXE_VENDOR_CLASS


def bytes_to_ip(addr: bytes) -> str:
    return ".".join(str(b) for b in addr)


def reply_destination(request: DhcpRequest, received_from: tuple[str, int]) -> tuple[str, int]:
    """DHCP 응답을 어디로 보낼지 결정한다(RFC 2131 4.1).

    릴레이 경유(giaddr != 0)면 relay:67로 유니캐스트, 아니면 broadcast 플래그가
    켜져 있으면 255.255.255.255:68로 브로드캐스트, 둘 다 아니면 받은 곳으로 회신한다
    (클라이언트가 이미 임시 IP로 통신 중인 REQUEST 갱신 등의 경우).
    """
    if request.giaddr != ZERO_IP:
        return (bytes_to_ip(request.giaddr), 67)
    if request.broadcast:
        return ("255.255.255.255", 68)
    return received_from


def build_pxe_options(tftp_server_ip: str, boot_filename: str) -> bytes:
    """옵션 66(next-server)/67(boot filename)만 담은 조각. Proxy DHCP처럼
    전체 DHCP 패킷이 아니라 PXE 옵션만 필요한 경우에 재사용한다.
    """
    server_bytes = tftp_server_ip.encode()
    filename_bytes = boot_filename.encode() + b"\x00"
    return (
        bytes([OPT_TFTP_SERVER_NAME, len(server_bytes)]) + server_bytes
        + bytes([OPT_BOOTFILE_NAME, len(filename_bytes)]) + filename_bytes
    )


def build_reply(
    *,
    message_type: int,
    xid: bytes,
    chaddr: bytes,
    yiaddr: bytes,
    siaddr: bytes,
    server_id: bytes,
    boot_filename: str,
    lease_seconds: int | None = None,
    subnet_mask: bytes | None = None,
    router: bytes | None = None,
    pxe: bool = False,
    giaddr: bytes = ZERO_IP,
) -> bytes:
    """DHCPOFFER/ACK/NAK 등 서버 응답 패킷 하나를 조립한다.

    Standalone DHCP(IP 할당 포함)와 Proxy DHCP(옵션만 응답, yiaddr=0)가
    둘 다 이 함수 하나로 패킷을 만든다.
    """
    header = (
        bytes([2])  # op = BOOTREPLY
        + bytes([1])  # htype = Ethernet
        + bytes([6])  # hlen
        + bytes([0])  # hops
        + xid
        + b"\x00\x00"  # secs
        + b"\x00\x00"  # flags
        + b"\x00\x00\x00\x00"  # ciaddr
        + yiaddr
        + siaddr
        + giaddr
        + chaddr.ljust(16, b"\x00")[:16]
        + b"\x00" * 64  # sname
        + boot_filename.encode().ljust(128, b"\x00")[:128]
    )

    options = bytes([OPT_MESSAGE_TYPE, 1, message_type])
    options += bytes([OPT_SERVER_ID, 4]) + server_id
    if lease_seconds is not None:
        options += bytes([OPT_LEASE_TIME, 4]) + lease_seconds.to_bytes(4, "big")
    if subnet_mask is not None:
        options += bytes([OPT_SUBNET_MASK, 4]) + subnet_mask
    if router is not None:
        options += bytes([OPT_ROUTER, 4]) + router
    if pxe:
        options += bytes([OPT_VENDOR_CLASS, len(PXE_VENDOR_CLASS)]) + PXE_VENDOR_CLASS
        options += build_pxe_options(".".join(str(b) for b in siaddr), boot_filename)
    options += bytes([OPT_END])

    return header + MAGIC_COOKIE + options
