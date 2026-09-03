import sqlite3

import pytest

from diskless import db, models
from diskless.config import Config
from diskless.orchestration import session as session_mod


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    db.init_schema(c)
    return c


@pytest.fixture
def cfg(tmp_path) -> Config:
    return Config(
        db_path=tmp_path / "db.sqlite3",
        priv_helper_socket=tmp_path / "priv.sock",
        images_root=tmp_path / "images",
        cookie_secret="test",
    )


@pytest.fixture
def profile(conn, cfg) -> models.ImageProfile:
    (cfg.images_root / "win11").mkdir(parents=True)
    conn.execute("INSERT INTO image_profile (name, storage_dir) VALUES ('win11', 'win11')")
    conn.execute(
        "INSERT INTO image_version (profile_id, version_number, file_path, checksum, created_by) "
        "VALUES (1, 1, 'base-v1.img', 'deadbeef', 'admin')"
    )
    (cfg.images_root / "win11" / "base-v1.img").write_bytes(b"\x00" * 4096)
    conn.commit()
    return models.get_image_profile(conn, 1)


@pytest.fixture
def binding(conn, profile) -> models.ClientBinding:
    conn.execute(
        "INSERT INTO client_binding (client_mac, image_profile_id, assigned_version) VALUES (?, ?, ?)",
        ("aa:bb:cc:dd:ee:ff", profile.id, 1),
    )
    conn.commit()
    return models.lookup_client_binding_by_mac(conn, "aa:bb:cc:dd:ee:ff")


def _patch_priv_client(monkeypatch):
    calls: list[tuple] = []

    def attach_loop(sock_path, img_path, readonly=True):
        calls.append(("attach_loop", img_path, readonly))
        return f"/dev/loop-{len(calls)}"

    def detach_loop(sock_path, dev):
        calls.append(("detach_loop", dev))

    def create_snapshot(sock_path, name, origin_dev, overlay_dev, chunk_size=8):
        calls.append(("create_snapshot", name, origin_dev, overlay_dev))

    def remove_snapshot(sock_path, name):
        calls.append(("remove_snapshot", name))

    monkeypatch.setattr(session_mod.priv_client, "attach_loop", attach_loop)
    monkeypatch.setattr(session_mod.priv_client, "detach_loop", detach_loop)
    monkeypatch.setattr(session_mod.priv_client, "create_snapshot", create_snapshot)
    monkeypatch.setattr(session_mod.priv_client, "remove_snapshot", remove_snapshot)
    return calls


def _patch_lio(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(session_mod.lio, "register_lun", lambda **kw: calls.append(("register_lun", kw)))
    monkeypatch.setattr(
        session_mod.lio, "remove_lun", lambda wwn, iqn, lun_name: calls.append(("remove_lun", wwn, iqn, lun_name))
    )
    return calls


# ---- 클라이언트 세션(readonly) ----


def test_client_session_creates_overlay_and_snapshot(conn, cfg, binding, monkeypatch):
    priv_calls = _patch_priv_client(monkeypatch)
    lio_calls = _patch_lio(monkeypatch)

    iqn = session_mod.initiator_iqn_for_mac(cfg, binding.client_mac)
    s = session_mod.start_session(cfg, conn, binding, iqn, readonly=True)

    assert s.readonly is True
    assert s.snapshot_name is not None
    assert s.backing_dev == f"/dev/mapper/{s.snapshot_name}"
    assert s.overlay_path.exists()
    assert s.overlay_path.stat().st_size == 4096  # base 이미지와 같은 크기로 생성

    ops = [c[0] for c in priv_calls]
    assert ops == ["attach_loop", "attach_loop", "create_snapshot"]
    assert ("register_lun",) == (lio_calls[0][0],)

    row = conn.execute("SELECT * FROM active_session WHERE initiator_iqn = ?", (iqn,)).fetchone()
    assert row["client_mac"] == binding.client_mac
    assert row["readonly"] == 1


def test_client_session_end_removes_snapshot_and_overlay_file(conn, cfg, binding, monkeypatch):
    _patch_priv_client(monkeypatch)
    _patch_lio(monkeypatch)
    iqn = session_mod.initiator_iqn_for_mac(cfg, binding.client_mac)
    s = session_mod.start_session(cfg, conn, binding, iqn, readonly=True)
    overlay_path = s.overlay_path

    session_mod.end_session(cfg, conn, s, merge=False)

    assert not overlay_path.exists()
    row = conn.execute("SELECT * FROM active_session WHERE initiator_iqn = ?", (iqn,)).fetchone()
    assert row is None


# ---- 관리자 쓰기 세션 ----


def test_admin_session_copies_file_without_snapshot(conn, cfg, binding, profile, monkeypatch):
    priv_calls = _patch_priv_client(monkeypatch)
    _patch_lio(monkeypatch)

    s = session_mod.start_session(
        cfg, conn, binding, "iqn.admin:workstation1", readonly=False, admin_username="admin",
    )

    assert s.readonly is False
    assert s.admin_file_path.name == "base-v2.img"
    assert s.admin_file_path.exists()
    assert s.admin_file_path.read_bytes() == b"\x00" * 4096
    assert s.backing_dev == s.base_loop_dev

    ops = [c[0] for c in priv_calls]
    assert ops == ["attach_loop"]  # snapshot/overlay 없음 — 복사본을 직접 씀


def test_admin_session_without_additional_bytes_keeps_original_size(conn, cfg, binding, monkeypatch):
    _patch_priv_client(monkeypatch)
    _patch_lio(monkeypatch)
    s = session_mod.start_session(cfg, conn, binding, "iqn.admin:workstation1", readonly=False, admin_username="admin")
    assert s.admin_file_path.stat().st_size == 4096


def test_admin_session_grows_disk_by_additional_bytes(conn, cfg, binding, monkeypatch):
    _patch_priv_client(monkeypatch)
    _patch_lio(monkeypatch)

    s = session_mod.start_session(
        cfg, conn, binding, "iqn.admin:workstation1", readonly=False, admin_username="admin",
        additional_bytes=8192,
    )

    assert s.admin_file_path.stat().st_size == 4096 + 8192
    # 원본 base 데이터는 앞부분에 그대로, 뒤에 붙은 영역은 빈 공간(sparse, 0으로 읽힘)
    data = s.admin_file_path.read_bytes()
    assert data[:4096] == b"\x00" * 4096
    assert data[4096:] == b"\x00" * 8192


def test_admin_session_requires_username(conn, cfg, binding, monkeypatch):
    _patch_priv_client(monkeypatch)
    _patch_lio(monkeypatch)
    with pytest.raises(ValueError):
        session_mod.start_session(cfg, conn, binding, "iqn.admin:x", readonly=False)


def test_admin_session_merge_registers_new_image_version(conn, cfg, binding, profile, monkeypatch):
    _patch_priv_client(monkeypatch)
    _patch_lio(monkeypatch)
    s = session_mod.start_session(cfg, conn, binding, "iqn.admin:workstation1", readonly=False, admin_username="admin")
    s.admin_file_path.write_bytes(b"\x11" * 4096)  # 관리자가 이미지를 "수정"했다고 가정

    session_mod.end_session(cfg, conn, s, merge=True)

    row = conn.execute(
        "SELECT * FROM image_version WHERE profile_id = ? AND version_number = 2", (profile.id,)
    ).fetchone()
    assert row is not None
    assert row["file_path"] == "base-v2.img"
    assert row["created_by"] == "admin"
    assert len(row["checksum"]) == 64  # sha256 hex

    # 기존 v1 파일은 그대로 보존돼야 함
    assert (cfg.images_root / "win11" / "base-v1.img").read_bytes() == b"\x00" * 4096


def test_admin_session_cancel_discards_copy_without_new_version(conn, cfg, binding, profile, monkeypatch):
    _patch_priv_client(monkeypatch)
    _patch_lio(monkeypatch)
    s = session_mod.start_session(cfg, conn, binding, "iqn.admin:workstation1", readonly=False, admin_username="admin")
    copy_path = s.admin_file_path

    session_mod.end_session(cfg, conn, s, merge=False)

    assert not copy_path.exists()
    row = conn.execute(
        "SELECT * FROM image_version WHERE profile_id = ? AND version_number = 2", (profile.id,)
    ).fetchone()
    assert row is None


def test_client_session_persists_fields_needed_to_reap_it_later(conn, cfg, binding, monkeypatch):
    """세션 리퍼가 initiator_iqn만으로 loop/snapshot을 되찾아 지울 수 있어야 하므로,
    이 정보들이 active_session에 그대로 저장돼야 한다."""
    _patch_priv_client(monkeypatch)
    _patch_lio(monkeypatch)
    iqn = session_mod.initiator_iqn_for_mac(cfg, binding.client_mac)

    s = session_mod.start_session(cfg, conn, binding, iqn, readonly=True)

    row = conn.execute("SELECT * FROM active_session WHERE initiator_iqn = ?", (iqn,)).fetchone()
    assert row["snapshot_name"] == s.snapshot_name
    assert row["base_loop_dev"] == s.base_loop_dev
    assert row["overlay_loop_dev"] == s.overlay_loop_dev
    assert row["overlay_path"] == str(s.overlay_path)


# ---- 세션 리퍼(연결 끊긴 클라이언트 세션 정리) ----


def test_reap_ends_session_whose_iscsi_login_is_gone(conn, cfg, binding, monkeypatch):
    priv_calls = _patch_priv_client(monkeypatch)
    lio_calls = _patch_lio(monkeypatch)
    iqn = session_mod.initiator_iqn_for_mac(cfg, binding.client_mac)
    session_mod.start_session(cfg, conn, binding, iqn, readonly=True)
    priv_calls.clear()
    lio_calls.clear()

    monkeypatch.setattr(session_mod.lio, "is_session_active", lambda wwn, initiator_iqn: False)

    reaped = session_mod.reap_disconnected_sessions(cfg, conn)

    assert reaped == [iqn]
    assert conn.execute("SELECT * FROM active_session WHERE initiator_iqn = ?", (iqn,)).fetchone() is None
    assert ("remove_lun", cfg.iscsi_target_wwn, iqn, f"session-{iqn.replace(':', '_')}") in lio_calls
    ops = [c[0] for c in priv_calls]
    assert "remove_snapshot" in ops
    assert ops.count("detach_loop") == 2  # base loop + overlay loop


def test_reap_leaves_still_connected_sessions_alone(conn, cfg, binding, monkeypatch):
    priv_calls = _patch_priv_client(monkeypatch)
    _patch_lio(monkeypatch)
    iqn = session_mod.initiator_iqn_for_mac(cfg, binding.client_mac)
    session_mod.start_session(cfg, conn, binding, iqn, readonly=True)
    priv_calls.clear()

    monkeypatch.setattr(session_mod.lio, "is_session_active", lambda wwn, initiator_iqn: True)

    reaped = session_mod.reap_disconnected_sessions(cfg, conn)

    assert reaped == []
    assert conn.execute("SELECT * FROM active_session WHERE initiator_iqn = ?", (iqn,)).fetchone() is not None
    assert priv_calls == []


def test_reap_skips_admin_sessions_even_if_disconnected(conn, cfg, binding, profile, monkeypatch):
    priv_calls = _patch_priv_client(monkeypatch)
    _patch_lio(monkeypatch)
    session_mod.start_session(cfg, conn, binding, "iqn.admin:workstation1", readonly=False, admin_username="admin")
    priv_calls.clear()

    monkeypatch.setattr(session_mod.lio, "is_session_active", lambda wwn, initiator_iqn: False)

    reaped = session_mod.reap_disconnected_sessions(cfg, conn)

    assert reaped == []  # 관리자 세션은 Web UI의 명시적 완료/취소로만 정리됨
    assert conn.execute("SELECT * FROM active_session WHERE initiator_iqn = 'iqn.admin:workstation1'").fetchone() is not None
    assert priv_calls == []


def test_reap_handles_multiple_client_sessions_independently(conn, cfg, binding, profile, monkeypatch):
    conn.execute(
        "INSERT INTO client_binding (client_mac, image_profile_id, assigned_version) VALUES (?, ?, ?)",
        ("11:11:11:11:11:11", profile.id, 1),
    )
    conn.commit()
    binding_a = binding
    binding_b = models.lookup_client_binding_by_mac(conn, "11:11:11:11:11:11")

    _patch_priv_client(monkeypatch)
    _patch_lio(monkeypatch)
    iqn_a = session_mod.initiator_iqn_for_mac(cfg, binding_a.client_mac)
    iqn_b = session_mod.initiator_iqn_for_mac(cfg, binding_b.client_mac)
    session_mod.start_session(cfg, conn, binding_a, iqn_a, readonly=True)
    session_mod.start_session(cfg, conn, binding_b, iqn_b, readonly=True)

    monkeypatch.setattr(
        session_mod.lio, "is_session_active", lambda wwn, initiator_iqn: initiator_iqn == iqn_b
    )

    reaped = session_mod.reap_disconnected_sessions(cfg, conn)

    assert reaped == [iqn_a]
    assert conn.execute("SELECT * FROM active_session WHERE initiator_iqn = ?", (iqn_a,)).fetchone() is None
    assert conn.execute("SELECT * FROM active_session WHERE initiator_iqn = ?", (iqn_b,)).fetchone() is not None
