import sqlite3

import pytest

from diskless import db
from diskless.bootd.web import context, image_profiles
from diskless.config import Config
from diskless.orchestration import lock
from tests.bootd.fake_request import FakeRequest

ADMIN_SESSION = {"user": "admin", "csrf": "tok"}


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    db.init_schema(c)
    c.execute("INSERT INTO image_profile (name, storage_dir) VALUES ('win11', 'win11')")
    c.commit()
    return c


@pytest.fixture(autouse=True)
def setup_context(conn, tmp_path):
    cfg = Config(db_path=tmp_path / "db.sqlite3", images_root=tmp_path / "images", cookie_secret="test")
    context.configure(cfg, conn)
    image_profiles._admin_sessions.clear()
    yield


def _csrf_request(**args_bytes) -> FakeRequest:
    args = {b"_csrf": [b"tok"]}
    for k, v in args_bytes.items():
        args[k.encode()] = [v.encode() if isinstance(v, str) else v]
    return FakeRequest(args=args)


def test_start_update_session_acquires_lock_and_starts_session(conn, monkeypatch):
    started = {}

    def fake_start_session(cfg, conn, binding, initiator_iqn, *, readonly, admin_username, additional_bytes=0):
        started["binding"] = binding
        started["readonly"] = readonly
        started["admin_username"] = admin_username
        started["additional_bytes"] = additional_bytes
        return "fake-session"

    monkeypatch.setattr(image_profiles.session_mod, "start_session", fake_start_session)

    request = _csrf_request(base_version="1")
    reply = image_profiles.start_update_session.__wrapped__(request, ADMIN_SESSION, 1)

    assert request.response_code in (None, 302)
    assert lock.is_locked(conn, 1)
    assert started["readonly"] is False
    assert started["admin_username"] == "admin"
    assert started["additional_bytes"] == 0  # 폼에 additional_gb 없으면 기본 0
    assert started["binding"].assigned_version == 1
    assert image_profiles._admin_sessions[1] == "fake-session"


def test_start_update_session_converts_additional_gb_to_bytes(conn, monkeypatch):
    started = {}

    def fake_start_session(cfg, conn, binding, initiator_iqn, *, readonly, admin_username, additional_bytes=0):
        started["additional_bytes"] = additional_bytes
        return "fake-session"

    monkeypatch.setattr(image_profiles.session_mod, "start_session", fake_start_session)

    request = _csrf_request(base_version="1", additional_gb="20")
    image_profiles.start_update_session.__wrapped__(request, ADMIN_SESSION, 1)

    assert started["additional_bytes"] == 20 * 1024**3


def test_start_update_session_conflicts_when_already_locked(conn, monkeypatch):
    lock.acquire(conn, 1, "other-admin")
    monkeypatch.setattr(image_profiles.session_mod, "start_session", lambda *a, **kw: pytest.fail("should not start"))

    request = _csrf_request(base_version="1")
    image_profiles.start_update_session.__wrapped__(request, ADMIN_SESSION, 1)

    assert request.response_code == 409


def test_start_update_session_rejects_bad_csrf(conn, monkeypatch):
    monkeypatch.setattr(image_profiles.session_mod, "start_session", lambda *a, **kw: pytest.fail("should not start"))
    request = FakeRequest(args={b"_csrf": [b"wrong"], b"base_version": [b"1"]})

    image_profiles.start_update_session.__wrapped__(request, ADMIN_SESSION, 1)

    assert request.response_code == 403
    assert not lock.is_locked(conn, 1)


def test_finish_update_session_merge_releases_lock(conn, monkeypatch):
    lock.acquire(conn, 1, "admin")
    image_profiles._admin_sessions[1] = "fake-session"
    ended = {}
    monkeypatch.setattr(
        image_profiles.session_mod, "end_session",
        lambda cfg, conn, session, *, merge: ended.update(session=session, merge=merge),
    )

    request = _csrf_request(action="merge")
    image_profiles.finish_update_session.__wrapped__(request, ADMIN_SESSION, 1)

    assert ended == {"session": "fake-session", "merge": True}
    assert not lock.is_locked(conn, 1)
    assert 1 not in image_profiles._admin_sessions


def test_finish_update_session_cancel_passes_merge_false(conn, monkeypatch):
    lock.acquire(conn, 1, "admin")
    image_profiles._admin_sessions[1] = "fake-session"
    ended = {}
    monkeypatch.setattr(
        image_profiles.session_mod, "end_session",
        lambda cfg, conn, session, *, merge: ended.update(merge=merge),
    )

    request = _csrf_request(action="cancel")
    image_profiles.finish_update_session.__wrapped__(request, ADMIN_SESSION, 1)

    assert ended == {"merge": False}


def test_finish_update_session_without_active_session_returns_404(conn):
    request = _csrf_request(action="merge")
    image_profiles.finish_update_session.__wrapped__(request, ADMIN_SESSION, 1)
    assert request.response_code == 404
