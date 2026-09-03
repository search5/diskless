import sqlite3

import pytest

from diskless import db
from diskless.bootd.web import context, dashboard
from diskless.config import Config
from diskless.webcookie import new_session_payload, set_session_cookie
from tests.bootd.fake_request import FakeRequest


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    db.init_schema(c)
    return c


@pytest.fixture(autouse=True)
def setup_context(conn, tmp_path):
    cfg = Config(db_path=tmp_path / "db.sqlite3", images_root=tmp_path / "images", cookie_secret="test-secret")
    context.configure(cfg, conn)
    yield


def _authenticated_request() -> FakeRequest:
    """set_session_cookie로 발급되는 토큰을 그대로 요청 쿠키로 되돌려줘 로그인 상태를 흉내낸다."""
    scratch = FakeRequest()
    set_session_cookie(scratch, context.cfg.cookie_secret, new_session_payload("admin"))
    token = scratch.set_cookies[0]["value"]
    return FakeRequest(cookies={b"diskless_session": token.encode()})


def test_dashboard_redirects_when_not_authenticated(monkeypatch):
    called = []
    monkeypatch.setattr(dashboard, "render_template", lambda *a, **kw: called.append(1) or b"")
    request = FakeRequest()

    dashboard.dashboard(request)

    assert request.response_code == 302
    assert request.headers[b"Location"] == b"/login"
    assert called == []


def test_dashboard_renders_when_authenticated(monkeypatch):
    captured = {}
    monkeypatch.setattr(dashboard, "render_template", lambda request, name, **kw: captured.update(name=name, **kw) or b"")

    dashboard.dashboard(_authenticated_request())

    assert captured["name"] == "dashboard.html"
    assert captured["session"]["user"] == "admin"
