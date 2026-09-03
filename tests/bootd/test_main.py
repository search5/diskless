"""bootd/main.py 배선 로직 검증. 실제 소켓 I/O 대신 reactor의
adoptDatagramPort/listenUDP/listenTCP를 모킹해서 "어떤 fd/포트에 어떤
프로토콜을 붙이는지"만 확인한다 — 실제 커널 소켓 동작은 검증 대상이 아니다.
"""

from __future__ import annotations

import sqlite3

import pytest
from twisted.internet import task as twisted_task

from diskless import db
from diskless.bootd import main as main_mod
from diskless.bootd.dhcp_proxy import ProxyDhcpProtocol
from diskless.bootd.dhcp_standalone import StandaloneDhcpProtocol
from diskless.bootd.tftp import TftpProtocol
from diskless.config import Config


def _cfg(tmp_path, **overrides) -> Config:
    defaults = dict(
        db_path=tmp_path / "db.sqlite3",
        tftp_root=tmp_path,
        images_root=tmp_path / "images",
        cookie_secret="test",
    )
    defaults.update(overrides)
    return Config(**defaults)


def _conn(cfg: Config) -> sqlite3.Connection:
    conn = db.connect(cfg.db_path)
    db.init_schema(conn)
    return conn


class FakeLoopingCall:
    """task.LoopingCall 대역 — 실제 reactor에 반복 타이머를 등록하지 않는다
    (안 그러면 이 테스트 프로세스의 전역 reactor에 취소 안 된 타이머가 계속 쌓임)."""

    instances: list["FakeLoopingCall"] = []

    def __init__(self, f):
        self.f = f
        self.interval = None
        FakeLoopingCall.instances.append(self)

    def start(self, interval, now=True):
        self.interval = interval


@pytest.fixture(autouse=True)
def fake_looping_call(monkeypatch):
    FakeLoopingCall.instances.clear()
    monkeypatch.setattr(twisted_task, "LoopingCall", FakeLoopingCall)
    yield


# ---- _start_dhcp_tftp ----


def test_raises_when_tftp_fd_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(main_mod, "systemd_fds_by_name", lambda: {})
    cfg = _cfg(tmp_path, dhcp_mode="proxy")
    with pytest.raises(RuntimeError):
        main_mod._start_dhcp_tftp(cfg, _conn(cfg))


def test_standalone_mode_requires_dhcp_fd_too(tmp_path, monkeypatch):
    monkeypatch.setattr(main_mod, "systemd_fds_by_name", lambda: {"tftp": 3})
    cfg = _cfg(tmp_path, dhcp_mode="standalone")
    with pytest.raises(RuntimeError):
        main_mod._start_dhcp_tftp(cfg, _conn(cfg))


def test_proxy_mode_does_not_require_dhcp_fd(tmp_path, monkeypatch):
    monkeypatch.setattr(main_mod, "systemd_fds_by_name", lambda: {"tftp": 3})
    monkeypatch.setattr(main_mod.reactor, "adoptDatagramPort", lambda *a, **kw: None)
    monkeypatch.setattr(main_mod.reactor, "listenUDP", lambda *a, **kw: None)
    cfg = _cfg(tmp_path, dhcp_mode="proxy")
    main_mod._start_dhcp_tftp(cfg, _conn(cfg))  # 예외 없이 끝나야 함


def test_unknown_dhcp_mode_raises_value_error(tmp_path, monkeypatch):
    monkeypatch.setattr(main_mod, "systemd_fds_by_name", lambda: {"tftp": 3})
    monkeypatch.setattr(main_mod.reactor, "adoptDatagramPort", lambda *a, **kw: None)
    cfg = _cfg(tmp_path, dhcp_mode="bogus")
    with pytest.raises(ValueError):
        main_mod._start_dhcp_tftp(cfg, _conn(cfg))


def test_proxy_mode_adopts_tftp_and_listens_udp_for_proxy_dhcp(tmp_path, monkeypatch):
    adopted = []
    listened = []
    monkeypatch.setattr(main_mod, "systemd_fds_by_name", lambda: {"tftp": 7})
    monkeypatch.setattr(main_mod.reactor, "adoptDatagramPort", lambda fd, fam, proto: adopted.append((fd, proto)))
    monkeypatch.setattr(main_mod.reactor, "listenUDP", lambda port, proto: listened.append((port, proto)))

    cfg = _cfg(tmp_path, dhcp_mode="proxy")
    main_mod._start_dhcp_tftp(cfg, _conn(cfg))

    assert len(adopted) == 1
    fd, tftp_proto = adopted[0]
    assert fd == 7
    assert isinstance(tftp_proto, TftpProtocol)

    assert len(listened) == 1
    port, proxy_proto = listened[0]
    assert port == main_mod.PROXY_DHCP_PORT
    assert isinstance(proxy_proto, ProxyDhcpProtocol)


def test_standalone_mode_adopts_both_tftp_and_dhcp_fds(tmp_path, monkeypatch):
    adopted = []
    monkeypatch.setattr(main_mod, "systemd_fds_by_name", lambda: {"tftp": 7, "dhcp": 8})
    monkeypatch.setattr(main_mod.reactor, "adoptDatagramPort", lambda fd, fam, proto: adopted.append((fd, proto)))

    cfg = _cfg(tmp_path, dhcp_mode="standalone", dhcp_standalone_lease_pool="192.0.2.0/24")
    main_mod._start_dhcp_tftp(cfg, _conn(cfg))

    kinds = {fd: type(proto) for fd, proto in adopted}
    assert kinds[7] is TftpProtocol
    assert kinds[8] is StandaloneDhcpProtocol


# ---- _start_web ----


def test_start_web_listens_on_web_and_ws_ports(tmp_path, monkeypatch):
    listened = []
    monkeypatch.setattr(main_mod.reactor, "listenTCP", lambda port, factory: listened.append((port, factory)))

    cfg = _cfg(tmp_path, web_port=18080, ws_port=18081)
    main_mod._start_web(cfg, _conn(cfg))

    ports = [p for p, _f in listened]
    assert cfg.web_port in ports
    assert cfg.ws_port in ports


def test_start_web_requires_cookie_secret(tmp_path, monkeypatch):
    monkeypatch.setattr(main_mod.reactor, "listenTCP", lambda *a, **kw: None)
    cfg = _cfg(tmp_path, cookie_secret="")
    with pytest.raises(RuntimeError):
        main_mod._start_web(cfg, _conn(cfg))


def test_start_web_registers_looping_call_for_session_poll(tmp_path, monkeypatch):
    monkeypatch.setattr(main_mod.reactor, "listenTCP", lambda *a, **kw: None)
    cfg = _cfg(tmp_path)
    main_mod._start_web(cfg, _conn(cfg))

    assert len(FakeLoopingCall.instances) == 1
    assert FakeLoopingCall.instances[0].interval == main_mod.SESSION_POLL_INTERVAL_S


# ---- _start_session_reaper ----


def test_start_session_reaper_registers_looping_call(tmp_path):
    cfg = _cfg(tmp_path)
    main_mod._start_session_reaper(cfg, _conn(cfg))

    assert len(FakeLoopingCall.instances) == 1
    assert FakeLoopingCall.instances[0].interval == main_mod.REAP_INTERVAL_S


def test_start_session_reaper_callback_invokes_reap_via_deferred(tmp_path, monkeypatch):
    from twisted.internet import defer

    calls = []
    monkeypatch.setattr(main_mod, "deferToThread", lambda f, *a, **kw: defer.execute(f, *a, **kw))
    monkeypatch.setattr(
        main_mod.session_mod, "reap_disconnected_sessions", lambda cfg, conn: calls.append((cfg, conn)) or []
    )
    cfg = _cfg(tmp_path)
    conn = _conn(cfg)

    main_mod._start_session_reaper(cfg, conn)
    FakeLoopingCall.instances[0].f()  # LoopingCall이 나중에 호출할 콜백을 지금 바로 실행

    assert calls == [(cfg, conn)]


# ---- start() 전체 배선 ----


def _patch_all_reactor_listeners(monkeypatch):
    monkeypatch.setattr(main_mod, "systemd_fds_by_name", lambda: {"tftp": 3})
    monkeypatch.setattr(main_mod.reactor, "adoptDatagramPort", lambda *a, **kw: None)
    monkeypatch.setattr(main_mod.reactor, "listenUDP", lambda *a, **kw: None)
    monkeypatch.setattr(main_mod.reactor, "listenTCP", lambda *a, **kw: None)


def test_start_runs_without_raising_in_proxy_mode(tmp_path, monkeypatch):
    _patch_all_reactor_listeners(monkeypatch)
    cfg = _cfg(tmp_path, dhcp_mode="proxy")
    main_mod.start(cfg)  # 예외 없이 끝나야 함


def test_start_releases_orphaned_locks_left_from_previous_run(tmp_path, monkeypatch):
    _patch_all_reactor_listeners(monkeypatch)
    cfg = _cfg(tmp_path, dhcp_mode="proxy")

    seed_conn = _conn(cfg)
    seed_conn.execute("INSERT INTO image_profile (name, storage_dir) VALUES ('win11', 'win11')")
    seed_conn.execute("UPDATE image_profile SET locked_by = 'someone', locked_at = datetime('now') WHERE id = 1")
    seed_conn.commit()
    seed_conn.close()

    main_mod.start(cfg)

    check_conn = db.connect(cfg.db_path)
    row = check_conn.execute("SELECT locked_by FROM image_profile WHERE id = 1").fetchone()
    assert row["locked_by"] is None


def test_start_applies_db_stored_dhcp_mode_override(tmp_path, monkeypatch):
    fds_requested = {}
    monkeypatch.setattr(main_mod, "systemd_fds_by_name", lambda: {"tftp": 3, "dhcp": 4})
    monkeypatch.setattr(main_mod.reactor, "adoptDatagramPort", lambda fd, fam, proto: fds_requested.setdefault(fd, type(proto)))
    monkeypatch.setattr(main_mod.reactor, "listenUDP", lambda *a, **kw: None)
    monkeypatch.setattr(main_mod.reactor, "listenTCP", lambda *a, **kw: None)

    cfg = _cfg(tmp_path, dhcp_mode="proxy", dhcp_standalone_lease_pool="192.0.2.0/24")
    seed_conn = _conn(cfg)
    seed_conn.execute("INSERT INTO site_setting (key, value) VALUES ('dhcp_mode', 'standalone')")
    seed_conn.commit()
    seed_conn.close()

    main_mod.start(cfg)

    # cfg 자체는 proxy였지만 site_setting에 저장된 standalone이 이겨서 dhcp fd(4)도 adopt돼야 함
    assert fds_requested[4] is StandaloneDhcpProtocol
