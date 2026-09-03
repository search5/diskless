import sqlite3

import pytest

from diskless import db, models
from diskless.bootd.web import client_bindings, context
from diskless.config import Config
from tests.bootd.fake_request import FakeRequest

ADMIN_SESSION = {"user": "admin", "csrf": "tok"}


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    db.init_schema(c)
    c.execute("INSERT INTO image_profile (name, storage_dir) VALUES ('win11', 'win11')")
    c.execute(
        "INSERT INTO image_version (profile_id, version_number, file_path, checksum, created_by) "
        "VALUES (1, 1, 'base-v1.img', 'deadbeef', 'admin')"
    )
    c.commit()
    return c


@pytest.fixture(autouse=True)
def setup_context(conn, tmp_path):
    cfg = Config(db_path=tmp_path / "db.sqlite3", images_root=tmp_path / "images", cookie_secret="test")
    context.configure(cfg, conn)
    yield


def _csrf_request(**args_bytes) -> FakeRequest:
    args = {b"_csrf": [b"tok"]}
    for k, v in args_bytes.items():
        args[k.encode()] = [v.encode() if isinstance(v, str) else v]
    return FakeRequest(args=args)


def test_register_client_creates_binding(conn):
    request = _csrf_request(client_mac="aa:bb:cc:dd:ee:ff", image_profile_id="1", assigned_version="1")

    client_bindings.register_client.__wrapped__(request, ADMIN_SESSION)

    row = conn.execute("SELECT * FROM client_binding WHERE client_mac = 'aa:bb:cc:dd:ee:ff'").fetchone()
    assert row is not None
    assert row["image_profile_id"] == 1
    assert row["assigned_version"] == 1
    assert request.response_code in (None, 302)


def test_register_client_rejects_duplicate_mac(conn):
    first = _csrf_request(client_mac="aa:bb:cc:dd:ee:ff", image_profile_id="1", assigned_version="1")
    client_bindings.register_client.__wrapped__(first, ADMIN_SESSION)

    second = _csrf_request(client_mac="aa:bb:cc:dd:ee:ff", image_profile_id="1", assigned_version="1")
    client_bindings.register_client.__wrapped__(second, ADMIN_SESSION)

    assert second.response_code == 409
    rows = conn.execute("SELECT * FROM client_binding WHERE client_mac = 'aa:bb:cc:dd:ee:ff'").fetchall()
    assert len(rows) == 1


def test_register_client_rejects_bad_csrf(conn):
    request = FakeRequest(args={b"_csrf": [b"wrong"], b"client_mac": [b"aa:bb:cc:dd:ee:ff"]})
    client_bindings.register_client.__wrapped__(request, ADMIN_SESSION)

    assert request.response_code == 403
    assert conn.execute("SELECT * FROM client_binding").fetchone() is None


def test_list_bindings_passes_profiles_for_registration_form(conn, monkeypatch):
    request = FakeRequest()
    captured = {}

    def fake_render_template(request, name, **kwargs):
        captured.update(kwargs)
        return b""

    monkeypatch.setattr(client_bindings, "render_template", fake_render_template)
    client_bindings.list_bindings.__wrapped__(request, ADMIN_SESSION)

    assert "profiles" in captured
    assert [p.name for p in captured["profiles"]] == ["win11"]
