from pathlib import Path

import pytest

from diskless import db
from diskless.bootd.web import context, image_profiles
from diskless.config import Config
from diskless.orchestration import lock
from tests.bootd.fake_request import FakeRequest

ADMIN_SESSION = {"user": "admin", "csrf": "tok"}


@pytest.fixture
def conn():
    # db.connect()를 그대로 써서 FK enforcement(PRAGMA foreign_keys=ON) 등 실제
    # 커넥션 설정과 동일한 조건에서 삭제 시 FK 제약이 걸리는지 검증한다.
    c = db.connect(Path(":memory:"))
    db.init_schema(c)
    return c


@pytest.fixture
def cfg(tmp_path) -> Config:
    return Config(db_path=tmp_path / "db.sqlite3", images_root=tmp_path / "images", cookie_secret="test")


@pytest.fixture(autouse=True)
def setup_context(conn, cfg):
    context.configure(cfg, conn)
    image_profiles._admin_sessions.clear()
    yield


def _seed_profile_with_version(conn, cfg, version_number=1):
    (cfg.images_root / "win11").mkdir(parents=True, exist_ok=True)
    conn.execute("INSERT INTO image_profile (name, storage_dir) VALUES ('win11', 'win11')")
    conn.execute(
        "INSERT INTO image_version (profile_id, version_number, file_path, checksum, created_by) "
        "VALUES (1, ?, ?, 'deadbeef', 'admin')",
        (version_number, f"base-v{version_number}.img"),
    )
    (cfg.images_root / "win11" / f"base-v{version_number}.img").write_bytes(b"\x00")
    conn.commit()


def _csrf_request(**args_bytes) -> FakeRequest:
    args = {b"_csrf": [b"tok"]}
    for k, v in args_bytes.items():
        args[k.encode()] = [v.encode()]
    return FakeRequest(args=args)


# ---- 버전 삭제 ----


def test_delete_version_removes_row_and_file(conn, cfg):
    _seed_profile_with_version(conn, cfg, version_number=1)
    file_path = cfg.images_root / "win11" / "base-v1.img"
    assert file_path.exists()

    request = _csrf_request()
    image_profiles.delete_version.__wrapped__(request, ADMIN_SESSION, 1, 1)

    assert conn.execute("SELECT * FROM image_version WHERE profile_id = 1 AND version_number = 1").fetchone() is None
    assert not file_path.exists()


def test_delete_version_blocked_when_client_assigned(conn, cfg):
    _seed_profile_with_version(conn, cfg, version_number=1)
    conn.execute(
        "INSERT INTO client_binding (client_mac, image_profile_id, assigned_version) VALUES ('aa:bb:cc:dd:ee:ff', 1, 1)"
    )
    conn.commit()

    request = _csrf_request()
    image_profiles.delete_version.__wrapped__(request, ADMIN_SESSION, 1, 1)

    assert request.response_code == 409
    assert conn.execute("SELECT * FROM image_version WHERE profile_id = 1 AND version_number = 1").fetchone() is not None
    assert (cfg.images_root / "win11" / "base-v1.img").exists()


def test_delete_version_missing_returns_404(conn, cfg):
    _seed_profile_with_version(conn, cfg, version_number=1)
    request = _csrf_request()

    image_profiles.delete_version.__wrapped__(request, ADMIN_SESSION, 1, 99)

    assert request.response_code == 404


def test_delete_version_rejects_bad_csrf(conn, cfg):
    _seed_profile_with_version(conn, cfg, version_number=1)
    request = FakeRequest(args={b"_csrf": [b"wrong"]})

    image_profiles.delete_version.__wrapped__(request, ADMIN_SESSION, 1, 1)

    assert request.response_code == 403
    assert conn.execute("SELECT * FROM image_version WHERE profile_id = 1 AND version_number = 1").fetchone() is not None


# ---- 프로파일 삭제 ----


def test_delete_profile_without_versions_succeeds(conn, cfg):
    conn.execute("INSERT INTO image_profile (name, storage_dir) VALUES ('empty-profile', 'empty')")
    conn.commit()

    request = _csrf_request()
    image_profiles.delete_profile.__wrapped__(request, ADMIN_SESSION, 1)

    assert conn.execute("SELECT * FROM image_profile WHERE id = 1").fetchone() is None


def test_delete_profile_blocked_when_versions_exist(conn, cfg):
    _seed_profile_with_version(conn, cfg, version_number=1)

    request = _csrf_request()
    image_profiles.delete_profile.__wrapped__(request, ADMIN_SESSION, 1)

    assert request.response_code == 409
    assert conn.execute("SELECT * FROM image_profile WHERE id = 1").fetchone() is not None


def test_delete_profile_blocked_when_locked(conn, cfg):
    conn.execute("INSERT INTO image_profile (name, storage_dir) VALUES ('win11', 'win11')")
    conn.commit()
    lock.acquire(conn, 1, "someone-else")

    request = _csrf_request()
    image_profiles.delete_profile.__wrapped__(request, ADMIN_SESSION, 1)

    assert request.response_code == 409


# ---- 강제 해제(고아 락 복구) ----


def test_force_unlock_releases_lock_without_live_session(conn, cfg):
    conn.execute("INSERT INTO image_profile (name, storage_dir) VALUES ('win11', 'win11')")
    conn.commit()
    lock.acquire(conn, 1, "someone-else")

    request = _csrf_request()
    image_profiles.force_unlock.__wrapped__(request, ADMIN_SESSION, 1)

    assert lock.is_locked(conn, 1) is False


def test_force_unlock_blocked_when_this_process_has_live_session(conn, cfg):
    conn.execute("INSERT INTO image_profile (name, storage_dir) VALUES ('win11', 'win11')")
    conn.commit()
    lock.acquire(conn, 1, "admin")
    image_profiles._admin_sessions[1] = "fake-live-session"

    request = _csrf_request()
    image_profiles.force_unlock.__wrapped__(request, ADMIN_SESSION, 1)

    assert request.response_code == 409
    assert lock.is_locked(conn, 1) is True


def test_list_versions_flags_orphaned_lock(conn, cfg, monkeypatch):
    conn.execute("INSERT INTO image_profile (name, storage_dir) VALUES ('win11', 'win11')")
    conn.commit()
    lock.acquire(conn, 1, "someone-else")  # 이 프로세스 _admin_sessions엔 없음 = 고아 락

    captured = {}
    monkeypatch.setattr(image_profiles, "render_template", lambda request, name, **kw: captured.update(kw) or b"")
    image_profiles.list_versions.__wrapped__(FakeRequest(), ADMIN_SESSION, 1)

    assert captured["editing"] is False
    assert captured["orphaned_lock"] is True


def test_force_unlock_rejects_bad_csrf(conn, cfg):
    conn.execute("INSERT INTO image_profile (name, storage_dir) VALUES ('win11', 'win11')")
    conn.commit()
    lock.acquire(conn, 1, "someone-else")

    request = FakeRequest(args={b"_csrf": [b"wrong"]})
    image_profiles.force_unlock.__wrapped__(request, ADMIN_SESSION, 1)

    assert request.response_code == 403
    assert lock.is_locked(conn, 1) is True
    assert conn.execute("SELECT * FROM image_profile WHERE id = 1").fetchone() is not None
