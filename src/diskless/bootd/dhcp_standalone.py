"""모드 A: Standalone DHCP. DESIGN.md 3.1 — 기존 DHCP 서버가 없는 사이트용.

포트 67/68에서 완전한 DHCP 서버 역할(DISCOVER/OFFER/REQUEST/ACK, lease pool)을
직접 수행하며, 같은 응답에 PXE 옵션(60/66/67)을 동봉한다. 포트 바인딩은
systemd 소켓 활성화로 처리한다(6.3) — 이 프로세스는 bind()를 호출하지 않는다.

giaddr(릴레이 경유)/broadcast 플래그에 따라 응답 목적지를 결정하고(RFC 2131 4.1,
`common.reply_destination`), 임대는 OFFER 단계/확정(commit) 단계 각각 타이머로
관리한다(`LeasePool`). 브로드캐스트 응답을 실제로 보내려면 소켓에 SO_BROADCAST가
필요한데, systemd 소켓 유닛에 `Broadcast=yes`를 지정해야 한다(deploy/systemd 참고).
"""

from __future__ import annotations

import ipaddress
import time

from twisted.internet import protocol

from diskless.dhcp import common

LEASE_SECONDS = 3600
OFFER_TIMEOUT_SECONDS = 30  # DISCOVER는 받았지만 REQUEST/ACK으로 확정 안 된 임시 예약 유지 시간


class PoolExhaustedError(RuntimeError):
    pass


class LeasePool:
    """IP 주소 풀/임대 관리. Proxy 모드에는 없는, Standalone 모드 고유 책임.

    두 단계 타이머를 둔다 — OFFER만 받고 REQUEST가 안 오면 `offer_timeout_seconds`
    후 회수(클라이언트가 다른 서버를 골랐거나 재부팅한 경우), REQUEST/ACK으로
    확정(commit)되면 `lease_seconds` 동안 보호된다. `clock`은 테스트에서 실시간
    대기 없이 시간을 흘려보내기 위한 주입 지점(기본은 `time.time`).
    """

    def __init__(
        self,
        network: str,
        lease_seconds: int = LEASE_SECONDS,
        offer_timeout_seconds: int = OFFER_TIMEOUT_SECONDS,
        clock=time.time,
    ) -> None:
        net = ipaddress.ip_network(network, strict=False)
        self._pool = [str(host) for host in net.hosts()]
        self._mac_to_ip: dict[str, str] = {}
        self._ip_to_mac: dict[str, str] = {}
        self._expires_at: dict[str, float] = {}
        self.lease_seconds = lease_seconds
        self.offer_timeout_seconds = offer_timeout_seconds
        self._clock = clock

    def _reclaim_expired(self) -> None:
        now = self._clock()
        for mac in [m for m, exp in self._expires_at.items() if exp <= now]:
            self.release(mac)

    def offer(self, client_mac: str) -> str:
        self._reclaim_expired()
        if client_mac in self._mac_to_ip:
            self._expires_at[client_mac] = self._clock() + self.offer_timeout_seconds
            return self._mac_to_ip[client_mac]
        for ip in self._pool:
            if ip not in self._ip_to_mac:
                self._mac_to_ip[client_mac] = ip
                self._ip_to_mac[ip] = client_mac
                self._expires_at[client_mac] = self._clock() + self.offer_timeout_seconds
                return ip
        raise PoolExhaustedError(f"주소 풀 소진: {client_mac}")

    def commit(self, client_mac: str) -> str:
        ip = self._mac_to_ip.get(client_mac) or self.offer(client_mac)
        self._expires_at[client_mac] = self._clock() + self.lease_seconds
        return ip

    def release(self, client_mac: str) -> None:
        ip = self._mac_to_ip.pop(client_mac, None)
        if ip is not None:
            self._ip_to_mac.pop(ip, None)
        self._expires_at.pop(client_mac, None)


def _ip_to_bytes(ip: str) -> bytes:
    return bytes(int(part) for part in ip.split("."))


class StandaloneDhcpProtocol(protocol.DatagramProtocol):
    def __init__(self, server_ip: str, boot_filename: str, lease_pool: LeasePool) -> None:
        self.server_ip_bytes = _ip_to_bytes(server_ip)
        self.boot_filename = boot_filename
        self.lease_pool = lease_pool

    def startProtocol(self) -> None:
        # broadcast 플래그가 켜진 클라이언트에게 255.255.255.255로 회신하려면 필요
        # (systemd 소켓 유닛에도 Broadcast=yes가 있어야 실제로 허용됨, deploy/systemd 참고)
        self.transport.setBroadcastAllowed(True)

    def datagramReceived(self, data: bytes, addr: tuple[str, int]) -> None:
        request = common.parse_request(data)

        if request.message_type == common.MSG_DISCOVER:
            ip = self.lease_pool.offer(request.client_mac)
            reply = self._build(common.MSG_OFFER, request, ip)
        elif request.message_type == common.MSG_REQUEST:
            ip = self.lease_pool.commit(request.client_mac)
            reply = self._build(common.MSG_ACK, request, ip)
        else:
            return

        self.transport.write(reply, common.reply_destination(request, addr))

    def _build(self, message_type: int, request: common.DhcpRequest, ip: str) -> bytes:
        return common.build_reply(
            message_type=message_type,
            xid=request.xid,
            chaddr=request.chaddr,
            yiaddr=_ip_to_bytes(ip),
            siaddr=self.server_ip_bytes,
            server_id=self.server_ip_bytes,
            boot_filename=self.boot_filename,
            lease_seconds=LEASE_SECONDS,
            pxe=common.is_pxe_client(request),
            giaddr=request.giaddr,
        )
