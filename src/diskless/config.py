"""설정 로딩. DESIGN.md 1.3/6.4 기준 경로·소켓 상수를 한곳에 모은다."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field, replace
from pathlib import Path


@dataclass(frozen=True)
class Config:
    # SQLite: ImageProfile / ImageVersion / ClientBinding (DESIGN.md 3.7)
    db_path: Path = field(default_factory=lambda: Path(os.environ.get("DISKLESS_DB", "/var/lib/diskless/diskless.db")))

    # priv_helper.py 유닉스 소켓 (DESIGN.md 6.2)
    priv_helper_socket: Path = field(
        default_factory=lambda: Path(os.environ.get("DISKLESS_PRIV_SOCK", "/run/diskless/priv-helper.sock"))
    )

    # 이미지 저장 루트: <images_root>/<profile.storage_dir>/<file_path>
    images_root: Path = field(default_factory=lambda: Path(os.environ.get("DISKLESS_IMAGES_ROOT", "/images")))

    # Web UI (bootd 프로세스 안, Klein) — HTTP와 WebSocket을 별도 포트로 (DESIGN.md 3.5)
    web_port: int = int(os.environ.get("DISKLESS_WEB_PORT", "8080"))
    ws_port: int = int(os.environ.get("DISKLESS_WS_PORT", "8081"))
    cookie_secret: str = field(default_factory=lambda: os.environ.get("DISKLESS_COOKIE_SECRET", ""))

    # TFTP / DHCP — dhcp_mode: 사이트별로 "standalone" 또는 "proxy" 중 하나만 선택(3.1)
    tftp_root: Path = field(default_factory=lambda: Path(os.environ.get("DISKLESS_TFTP_ROOT", "/srv/tftp")))
    dhcp_mode: str = os.environ.get("DISKLESS_DHCP_MODE", "proxy")
    dhcp_boot_filename: str = os.environ.get("DISKLESS_DHCP_BOOT_FILENAME", "undionly.kpxe")
    dhcp_standalone_lease_pool: str = os.environ.get("DISKLESS_DHCP_POOL", "")

    # iSCSI — target은 서버당 하나로 고정, 세션마다 그 밑에 LUN+ACL만 추가한다(3.4)
    iscsi_target_iqn_prefix: str = os.environ.get("DISKLESS_IQN_PREFIX", "iqn.2026-09.local.diskless")
    iscsi_target_wwn: str = os.environ.get("DISKLESS_TARGET_WWN", "iqn.2026-09.local.diskless:server")
    iscsi_portal_ip: str = os.environ.get("DISKLESS_PORTAL_IP", "0.0.0.0")
    iscsi_portal_port: int = int(os.environ.get("DISKLESS_PORTAL_PORT", "3260"))


def load_config() -> Config:
    """공통 로더 — cookie_secret 검증은 bootd/main.py에서 별도로 한다."""
    return Config()


def require_cookie_secret(cfg: Config) -> None:
    if not cfg.cookie_secret:
        raise RuntimeError("DISKLESS_COOKIE_SECRET 환경변수가 필요합니다 (Web UI 세션 쿠키 서명용)")


DB_OVERRIDABLE_FIELDS = ("dhcp_mode",)


def apply_db_overrides(cfg: Config, conn: sqlite3.Connection) -> Config:
    """Web UI(3.5 DHCP 모드 설정 화면)에서 site_setting에 저장한 값으로 env var
    기본값을 덮어쓴다. bootd는 포트를 시작 시점에만 소켓 활성화로 받으므로,
    여기 반영되는 건 다음 프로세스 재시작부터다(6.5).
    """
    overrides = {}
    for field_name in DB_OVERRIDABLE_FIELDS:
        row = conn.execute("SELECT value FROM site_setting WHERE key = ?", (field_name,)).fetchone()
        if row is not None:
            overrides[field_name] = row["value"]
    return replace(cfg, **overrides) if overrides else cfg
