import sqlite3

import pytest

from diskless import db
from diskless.bootd.web import context, settings
from diskless.config import Config
from tests.bootd.fake_request import FakeRequest

ADMIN_SESSION = {"user": "admin", "csrf": "tok"}


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    db.init_schema(c)
    return c


@pytest.fixture(autouse=True)
def setup_context(conn, tmp_path):
    cfg = Config(db_path=tmp_path / "db.sqlite3", images_root=tmp_path / "images", cookie_secret="test")
    context.configure(cfg, conn)
    yield


def _csrf_request(**args_bytes) -> FakeRequest:
    args = {b"_csrf": [b"tok"]}
    for k, v in args_bytes.items():
        args[k.encode()] = [v.encode()]
    return FakeRequest(args=args)


def test_update_dhcp_mode_stores_setting(conn):
    request = _csrf_request(dhcp_mode="standalone")
    settings.update_settings.__wrapped__(request, ADMIN_SESSION)

    row = conn.execute("SELECT value FROM site_setting WHERE key = 'dhcp_mode'").fetchone()
    assert row["value"] == "standalone"


def test_update_dhcp_mode_upserts(conn):
    conn.execute("INSERT INTO site_setting (key, value) VALUES ('dhcp_mode', 'proxy')")
    conn.commit()

    request = _csrf_request(dhcp_mode="standalone")
    settings.update_settings.__wrapped__(request, ADMIN_SESSION)

    rows = conn.execute("SELECT * FROM site_setting WHERE key = 'dhcp_mode'").fetchall()
    assert len(rows) == 1
    assert rows[0]["value"] == "standalone"


def test_update_dhcp_mode_rejects_invalid_value(conn):
    request = _csrf_request(dhcp_mode="not-a-real-mode")
    settings.update_settings.__wrapped__(request, ADMIN_SESSION)

    assert request.response_code == 400
    assert conn.execute("SELECT * FROM site_setting").fetchone() is None


def test_update_dhcp_mode_rejects_bad_csrf(conn):
    request = FakeRequest(args={b"_csrf": [b"wrong"], b"dhcp_mode": [b"standalone"]})
    settings.update_settings.__wrapped__(request, ADMIN_SESSION)

    assert request.response_code == 403
    assert conn.execute("SELECT * FROM site_setting").fetchone() is None


def test_show_settings_passes_effective_and_stored_mode(conn, monkeypatch):
    conn.execute("INSERT INTO site_setting (key, value) VALUES ('dhcp_mode', 'standalone')")
    conn.commit()
    captured = {}
    monkeypatch.setattr(settings, "render_template", lambda request, name, **kw: captured.update(kw) or b"")

    settings.show_settings.__wrapped__(FakeRequest(), ADMIN_SESSION)

    assert captured["current_dhcp_mode"] == context.cfg.dhcp_mode  # 지금 떠 있는 프로세스가 실제 쓰는 값
    assert captured["stored_dhcp_mode"] == "standalone"  # DB에 저장된, 다음 재시작부터 적용될 값
