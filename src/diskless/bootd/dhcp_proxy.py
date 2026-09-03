"""모드 B: Proxy DHCP. DESIGN.md 3.1 — 기존 DHCP 서버가 있는 사이트용.

IP 할당은 기존 DHCP 서버가 계속 담당, 이 프로토콜은 포트 4011에서
IP 없이 PXE 옵션(60/66/67)만 응답한다(RFC 4578). 4011은 특권 포트가
아니므로 직접 bind해도 되고, 별도 권한 상승이 필요 없다.

응답 목적지는 Standalone DHCP(dhcp_standalone.py)와 동일하게
`common.reply_destination`(RFC 2131 4.1)으로 결정한다 — giaddr가 있으면
relay:67, 없고 broadcast 플래그가 켜져 있으면 255.255.255.255:68.
"""

from __future__ import annotations

from twisted.internet import protocol

from diskless.dhcp import common

PROXY_DHCP_PORT = 4011

ZERO_IP = b"\x00\x00\x00\x00"


class ProxyDhcpProtocol(protocol.DatagramProtocol):
    def __init__(self, server_ip: str, boot_filename: str) -> None:
        self.server_ip = server_ip
        self.server_ip_bytes = bytes(int(part) for part in server_ip.split("."))
        self.boot_filename = boot_filename

    def startProtocol(self) -> None:
        self.transport.setBroadcastAllowed(True)

    def datagramReceived(self, data: bytes, addr: tuple[str, int]) -> None:
        request = common.parse_request(data)
        if not common.is_pxe_client(request):
            return

        reply = common.build_reply(
            message_type=common.MSG_OFFER,
            xid=request.xid,
            chaddr=request.chaddr,
            yiaddr=ZERO_IP,  # Proxy DHCP는 IP를 할당하지 않음(3.1)
            siaddr=self.server_ip_bytes,
            server_id=self.server_ip_bytes,
            boot_filename=self.boot_filename,
            pxe=True,
            giaddr=request.giaddr,
        )
        self.transport.write(reply, common.reply_destination(request, addr))
