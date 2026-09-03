"""DatagramProtocol 테스트용 가짜 transport. 실제 소켓 없이 write() 호출만 기록한다."""

from __future__ import annotations


class FakeDatagramTransport:
    def __init__(self) -> None:
        self.sent: list[tuple[bytes, tuple[str, int]]] = []

    def write(self, data: bytes, addr: tuple[str, int]) -> None:
        self.sent.append((data, addr))
