"""priv_client <-> priv_helper.server 간 실제 소켓 왕복(JSON 프레이밍) 검증.

AF_UNIX 소켓은 macOS/Linux 어디서나 동작하므로 VM 없이도 실제 프로토콜을 확인할 수 있다.
"""

from __future__ import annotations

import socket
import tempfile
import threading
import uuid
from pathlib import Path

import pytest

from diskless.orchestration import priv_client
from diskless.priv_helper import server as priv_server


@pytest.fixture
def sock_path():
    """AF_UNIX 경로 길이 제한(macOS 104바이트) 때문에 pytest의 tmp_path(깊게 중첩됨) 대신
    /tmp 바로 밑에 짧은 경로를 쓴다."""
    path = Path(tempfile.gettempdir()) / f"diskless-test-{uuid.uuid4().hex[:8]}.sock"
    yield path
    path.unlink(missing_ok=True)


def _serve_one(sock_path: Path) -> socket.socket:
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sock_path))
    srv.listen(1)
    return srv


def test_call_priv_helper_round_trip_success(sock_path, monkeypatch):
    srv = _serve_one(sock_path)
    monkeypatch.setitem(priv_server.ALLOWED_OPS, "ping", lambda args: {"echo": args["value"]})

    thread = threading.Thread(target=lambda: priv_server._handle(srv.accept()[0]), daemon=True)
    thread.start()

    result = priv_client.call_priv_helper(sock_path, "ping", value=42)
    thread.join(timeout=2)
    srv.close()

    assert result == {"echo": 42}


def test_call_priv_helper_raises_on_disallowed_op(sock_path):
    srv = _serve_one(sock_path)

    thread = threading.Thread(target=lambda: priv_server._handle(srv.accept()[0]), daemon=True)
    thread.start()

    with pytest.raises(priv_client.PrivHelperError):
        priv_client.call_priv_helper(sock_path, "rm_rf_root")

    thread.join(timeout=2)
    srv.close()


def test_attach_loop_wrapper_passes_through(sock_path, monkeypatch):
    srv = _serve_one(sock_path)
    monkeypatch.setitem(priv_server.ALLOWED_OPS, "attach_loop", lambda args: "/dev/loop9")

    thread = threading.Thread(target=lambda: priv_server._handle(srv.accept()[0]), daemon=True)
    thread.start()

    result = priv_client.attach_loop(sock_path, "/images/base.img", readonly=True)
    thread.join(timeout=2)
    srv.close()

    assert result == "/dev/loop9"
