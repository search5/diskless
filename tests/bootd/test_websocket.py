import json

from diskless.bootd.websocket import SessionMonitorFactory, SessionMonitorProtocol, make_factory


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    def sendMessage(self, payload: bytes) -> None:
        self.sent.append(payload)


def test_make_factory_uses_session_monitor_protocol():
    factory = make_factory("ws://0.0.0.0:8081")
    assert factory.protocol is SessionMonitorProtocol


def test_make_factory_starts_with_no_clients():
    factory = make_factory("ws://0.0.0.0:8081")
    assert factory.clients == set()


def test_register_adds_client():
    factory = SessionMonitorFactory("ws://0.0.0.0:8081")
    client = FakeClient()
    factory.register(client)
    assert client in factory.clients


def test_unregister_removes_client():
    factory = SessionMonitorFactory("ws://0.0.0.0:8081")
    client = FakeClient()
    factory.register(client)
    factory.unregister(client)
    assert client not in factory.clients


def test_unregister_unknown_client_is_a_noop():
    factory = SessionMonitorFactory("ws://0.0.0.0:8081")
    factory.unregister(FakeClient())  # 등록 안 된 클라이언트라도 에러 없이 무시


def test_broadcast_sends_json_to_all_registered_clients():
    factory = SessionMonitorFactory("ws://0.0.0.0:8081")
    client_a, client_b = FakeClient(), FakeClient()
    factory.register(client_a)
    factory.register(client_b)

    sessions = [{"initiator_iqn": "iqn:client-a", "client_mac": "aa:bb:cc:dd:ee:ff"}]
    factory.broadcast(sessions)

    expected = json.dumps({"sessions": sessions}).encode()
    assert client_a.sent == [expected]
    assert client_b.sent == [expected]


def test_broadcast_does_not_reach_unregistered_client():
    factory = SessionMonitorFactory("ws://0.0.0.0:8081")
    client = FakeClient()
    factory.register(client)
    factory.unregister(client)

    factory.broadcast([{"a": 1}])

    assert client.sent == []


# ---- SessionMonitorProtocol 생명주기 ----


def test_on_open_registers_with_factory():
    factory = SessionMonitorFactory("ws://0.0.0.0:8081")
    proto = SessionMonitorProtocol()
    proto.factory = factory

    proto.onOpen()

    assert proto in factory.clients


def test_on_close_unregisters_from_factory():
    factory = SessionMonitorFactory("ws://0.0.0.0:8081")
    proto = SessionMonitorProtocol()
    proto.factory = factory
    proto.onOpen()

    proto.onClose(True, 1000, "connection closed")

    assert proto not in factory.clients
