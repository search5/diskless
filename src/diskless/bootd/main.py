"""통합 데몬 — DHCP(Standalone/Proxy) + TFTP + iSCSI 세션 오케스트레이션 +
관리 웹 UI(Klein)를 Twisted reactor 하나로 실행한다.

priv_helper(CAP_SYS_ADMIN)만 별도 프로세스로 분리 유지한다 — 네트워크
패킷/HTTP 요청을 직접 파싱하는 이 프로세스에 특권을 두지 않기 위함.
"""

from __future__ import annotations

import socket
import sys

from twisted.internet import reactor, task
from twisted.internet.threads import deferToThread
from twisted.logger import Logger, globalLogBeginner, textFileLogObserver
from twisted.web.server import Site

from diskless import db
from diskless.bootd.dhcp_proxy import PROXY_DHCP_PORT, ProxyDhcpProtocol
from diskless.bootd.dhcp_standalone import LeasePool, StandaloneDhcpProtocol
from diskless.bootd.systemd_sockets import systemd_fds_by_name
from diskless.bootd.tftp import TftpProtocol
from diskless.bootd.web import app as web_app
from diskless.bootd.web import context as web_context
from diskless.bootd.websocket import make_factory
from diskless.config import Config, apply_db_overrides, load_config, require_cookie_secret
from diskless.orchestration import lock
from diskless.orchestration import session as session_mod

# 라우트를 web_app에 등록시키기 위해 임포트만 하고 직접 쓰지는 않음
from diskless.bootd.web import auth as _auth  # noqa: F401
from diskless.bootd.web import client_bindings as _client_bindings  # noqa: F401
from diskless.bootd.web import dashboard as _dashboard  # noqa: F401
from diskless.bootd.web import image_profiles as _image_profiles  # noqa: F401
from diskless.bootd.web import settings as _settings  # noqa: F401

log = Logger()
SESSION_POLL_INTERVAL_S = 2.0
REAP_INTERVAL_S = 30.0


def _start_dhcp_tftp(cfg: Config, conn) -> None:
    fds = systemd_fds_by_name()
    required = {"tftp"} | ({"dhcp"} if cfg.dhcp_mode == "standalone" else set())
    if missing := required - fds.keys():
        raise RuntimeError(
            f"systemd 소켓 활성화로 fd를 못 받음: {missing} — diskless-bootd*.socket 유닛을 통해 "
            "실행해야 함(6.3 참고)"
        )

    tftp_proto = TftpProtocol(cfg, cfg.tftp_root, conn)
    reactor.adoptDatagramPort(fds["tftp"], socket.AF_INET, tftp_proto)

    if cfg.dhcp_mode == "standalone":
        lease_pool = LeasePool(cfg.dhcp_standalone_lease_pool)
        dhcp_proto = StandaloneDhcpProtocol(cfg.iscsi_portal_ip, cfg.dhcp_boot_filename, lease_pool)
        reactor.adoptDatagramPort(fds["dhcp"], socket.AF_INET, dhcp_proto)
    elif cfg.dhcp_mode == "proxy":
        proxy_proto = ProxyDhcpProtocol(cfg.iscsi_portal_ip, cfg.dhcp_boot_filename)
        reactor.listenUDP(PROXY_DHCP_PORT, proxy_proto)
    else:
        raise ValueError(f"알 수 없는 dhcp_mode: {cfg.dhcp_mode!r} (standalone/proxy 중 하나여야 함)")


def _start_web(cfg: Config, conn) -> None:
    require_cookie_secret(cfg)
    web_context.configure(cfg, conn)
    reactor.listenTCP(cfg.web_port, Site(web_app.resource()))

    ws_factory = make_factory(f"ws://0.0.0.0:{cfg.ws_port}")
    reactor.listenTCP(cfg.ws_port, ws_factory)

    def poll_and_broadcast() -> None:
        rows = conn.execute("SELECT * FROM active_session ORDER BY started_at").fetchall()
        ws_factory.broadcast([dict(r) for r in rows])

    task.LoopingCall(poll_and_broadcast).start(SESSION_POLL_INTERVAL_S)


def _start_session_reaper(cfg: Config, conn) -> None:
    """연결이 끊긴 클라이언트 iSCSI 세션(loop/snapshot/LUN)을 주기적으로 정리한다
    (orchestration/session.py: reap_disconnected_sessions) — rtslib 호출은 블로킹이라
    deferToThread로 돌린다.
    """

    def reap() -> None:
        d = deferToThread(session_mod.reap_disconnected_sessions, cfg, conn)
        d.addErrback(lambda f: log.failure("세션 정리 중 오류", failure=f))

    task.LoopingCall(reap).start(REAP_INTERVAL_S, now=False)


def start(cfg: Config | None = None) -> None:
    cfg = cfg or load_config()
    conn = db.connect(cfg.db_path)
    db.init_schema(conn)
    cfg = apply_db_overrides(cfg, conn)  # Web UI(DHCP 모드 설정 화면)가 저장한 값, 6.5

    # 막 시작한 프로세스라 살아있는 관리자 세션이 있을 수 없음 — 남아있는 락은
    # 전부 이전 프로세스가 비정상 종료하며 못 푼 고아 락이다(6.6).
    orphaned = lock.release_all(conn)
    if orphaned:
        log.warn(
            "시작 시 고아 락 해제: profile_ids={ids} — 이전 프로세스가 비정상 종료됐을 가능성. "
            "그 세션이 남겼을 수 있는 임시 이미지 파일은 수동으로 확인 필요",
            ids=orphaned,
        )

    # rtslib/subprocess 등 블로킹 호출은 deferToThread로 돌린다(6.1/6.4) — reactor가
    # 첫 deferToThread 호출 시 내부 스레드풀을 알아서 기동하므로 별도 설정 불필요.
    _start_dhcp_tftp(cfg, conn)
    _start_web(cfg, conn)
    _start_session_reaper(cfg, conn)  # 연결 끊긴 클라이언트 세션 정리(loop/snapshot/LUN)

    log.info("diskless-bootd 시작: dhcp_mode={mode}", mode=cfg.dhcp_mode)


def main() -> None:
    globalLogBeginner.beginLoggingTo([textFileLogObserver(sys.stdout)])
    start()
    reactor.run()


if __name__ == "__main__":
    main()
