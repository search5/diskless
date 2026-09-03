import sqlite3

import pytest

from diskless import db
from diskless.bootd.web import auth, context
from diskless.cli.create_admin import create_admin
from diskless.config import Config
from tests.bootd.fake_request import FakeRequest


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    db.init_schema(c)
    create_admin(c, "admin", "correct-password")
    return c


@pytest.fixture(autouse=True)
def setup_context(conn, tmp_path):
    cfg = Config(db_path=tmp_path / "db.sqlite3", images_root=tmp_path / "images", cookie_secret="test-secret")
    context.configure(cfg, conn)
    yield


def _login_request(username: str, password: str) -> FakeRequest:
    return FakeRequest(args={b"username": [username.encode()], b"password": [password.encode()]})


def test_login_form_renders_without_error(monkeypatch):
    captured = {}
    monkeypatch.setattr(auth, "render_template", lambda request, name, **kw: captured.update(kw) or b"")

    auth.login_form(FakeRequest())

    assert captured["error"] is None


def test_login_submit_with_correct_password_sets_cookie_and_redirects():
    request = _login_request("admin", "correct-password")

    auth.login_submit(request)

    assert request.response_code == 302
    assert request.headers[b"Location"] == b"/"
    assert any(c["name"] == "diskless_session" for c in request.set_cookies)


def test_login_submit_with_wrong_password_shows_error_without_cookie(monkeypatch):
    captured = {}
    monkeypatch.setattr(auth, "render_template", lambda request, name, **kw: captured.update(kw) or b"")
    request = _login_request("admin", "wrong-password")

    auth.login_submit(request)

    assert captured["error"] is not None
    assert request.set_cookies == []


def test_login_submit_unknown_user_shows_error(monkeypatch):
    captured = {}
    monkeypatch.setattr(auth, "render_template", lambda request, name, **kw: captured.update(kw) or b"")
    request = _login_request("nobody", "whatever")

    auth.login_submit(request)

    assert captured["error"] is not None


def test_logout_clears_cookie_and_redirects():
    request = FakeRequest()

    auth.logout(request)

    assert request.response_code == 302
    assert request.headers[b"Location"] == b"/login"
    assert any(c["name"] == "diskless_session" and c["value"] == "" for c in request.set_cookies)
