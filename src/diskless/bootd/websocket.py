"""세션 모니터 WebSocket. DESIGN.md 3.5 화면 1번 — autobahn(Twisted용 WebSocket).

Klein(HTTP)과 별도 TCP 포트로 띄운다 — 같은 reactor/프로세스 안이므로
active_session 테이블 상태를 바로 조회해 방송할 수 있다(더 이상 프로세스 간
폴링이 필요 없음, bootd/main.py의 LoopingCall이 그래도 주기적으로 갱신한다).
"""

from __future__ import annotations

import json

from autobahn.twisted.websocket import WebSocketServerFactory, WebSocketServerProtocol


class SessionMonitorProtocol(WebSocketServerProtocol):
    def onOpen(self) -> None:
        self.factory.register(self)

    def onClose(self, wasClean: bool, code: int, reason: str) -> None:
        self.factory.unregister(self)


class SessionMonitorFactory(WebSocketServerFactory):
    protocol = SessionMonitorProtocol

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.clients: set[SessionMonitorProtocol] = set()

    def register(self, client: SessionMonitorProtocol) -> None:
        self.clients.add(client)

    def unregister(self, client: SessionMonitorProtocol) -> None:
        self.clients.discard(client)

    def broadcast(self, sessions: list[dict]) -> None:
        payload = json.dumps({"sessions": sessions}).encode()
        for client in self.clients:
            client.sendMessage(payload)


def make_factory(url: str) -> SessionMonitorFactory:
    return SessionMonitorFactory(url)
