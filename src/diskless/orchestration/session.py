"""세션 생명주기. DESIGN.md 3.4/3.7.

**클라이언트 세션**(readonly=True): base 이미지(읽기전용) + 세션별 CoW 오버레이를
dm-snapshot으로 묶어서 export. 세션 종료 시 오버레이를 통째로 버린다(3.5).

**관리자 쓰기 세션**(readonly=False): base 이미지를 복사(가능하면 reflink, 3.4/6.2)한
새 파일을 직접 쓰기 가능한 loop로 export한다 — dm-snapshot/merge를 쓰지 않는다.
커널 snapshot-merge는 origin을 그 자리에서 변형시키므로 "기존 버전은 그대로
보존"이라는 요구사항과 맞지 않기 때문이다. 세션 종료 시 그 복사본 자체가 새
`ImageVersion`이 된다(merge=True) — 취소하면 복사본만 버린다(merge=False).
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path

from diskless import models
from diskless.config import Config
from diskless.iscsi import lio
from diskless.orchestration import priv_client

OVERLAY_DIR_NAME = "_overlays"


@dataclass(frozen=True)
class Session:
    initiator_iqn: str
    lun_name: str  # LIO 백스토어/LUN 식별자. target 자체는 cfg.iscsi_target_wwn으로 고정(3.4)
    readonly: bool
    backing_dev: str
    base_loop_dev: str
    profile_id: int
    version_number: int
    # 클라이언트 세션 전용
    overlay_loop_dev: str | None = None
    overlay_path: Path | None = None
    snapshot_name: str | None = None
    # 관리자 세션 전용
    admin_file_path: Path | None = None
    admin_username: str | None = None


def _safe_id(text: str) -> str:
    return text.replace(":", "_").replace("/", "_")


def initiator_iqn_for_mac(cfg: Config, client_mac: str) -> str:
    """클라이언트 initiator IQN을 MAC에서 결정론적으로 만든다 — 이 값을 sanboot
    스크립트의 `set initiator-iqn`과 LUN ACL 등록에 동일하게 사용해 모호함을 없앤다.
    """
    return f"{cfg.iscsi_target_iqn_prefix}:client-{client_mac.replace(':', '')}"


def _overlay_path(cfg: Config, session_id: str) -> Path:
    directory = cfg.images_root / OVERLAY_DIR_NAME
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{_safe_id(session_id)}.cow"


def _create_sparse_overlay(overlay_path: Path, size_bytes: int) -> None:
    with open(overlay_path, "wb") as f:
        f.truncate(size_bytes)


def _reflink_or_copy(src: Path, dst: Path) -> None:
    """가능하면 reflink(즉시 완료, 디스크 추가 사용 없음 — Btrfs/XFS)로 복사,
    안 되면(ext4 등) 일반 복사로 폴백한다."""
    try:
        subprocess.run(["cp", "--reflink=always", str(src), str(dst)], check=True, capture_output=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        shutil.copy2(src, dst)


def _grow_file(path: Path, additional_bytes: int) -> None:
    """디스크를 base+추가 용량으로 키운다(사용자 요청) — 파일 하나를 그냥 뒤로
    늘리는 것뿐이라 dm-linear로 별도 파일을 이어붙이는 것보다 훨씬 단순하다.
    새로 늘어난 구간은 스파스(0으로 읽힘)라 실제 디스크를 즉시 소비하지 않는다.
    """
    if additional_bytes <= 0:
        return
    new_size = path.stat().st_size + additional_bytes
    with open(path, "r+b") as f:
        f.truncate(new_size)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _next_version_number(conn: sqlite3.Connection, profile_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(version_number), 0) AS m FROM image_version WHERE profile_id = ?", (profile_id,)
    ).fetchone()
    return row["m"] + 1


def _start_client_session(
    cfg: Config, conn: sqlite3.Connection, binding: models.ClientBinding, profile: models.ImageProfile, initiator_iqn: str
) -> Session:
    img_path = models.resolve_image_path(cfg.images_root, profile, binding.assigned_version, conn)
    base_loop_dev = priv_client.attach_loop(cfg.priv_helper_socket, str(img_path), readonly=True)

    overlay_path = _overlay_path(cfg, initiator_iqn)
    _create_sparse_overlay(overlay_path, img_path.stat().st_size)
    overlay_loop_dev = priv_client.attach_loop(cfg.priv_helper_socket, str(overlay_path), readonly=False)

    snapshot_name = f"session-{_safe_id(initiator_iqn)}"
    priv_client.create_snapshot(
        cfg.priv_helper_socket, snapshot_name, origin_dev=base_loop_dev, overlay_dev=overlay_loop_dev
    )

    return Session(
        initiator_iqn=initiator_iqn,
        lun_name=snapshot_name,
        readonly=True,
        backing_dev=f"/dev/mapper/{snapshot_name}",
        base_loop_dev=base_loop_dev,
        profile_id=profile.id,
        version_number=binding.assigned_version,
        overlay_loop_dev=overlay_loop_dev,
        overlay_path=overlay_path,
        snapshot_name=snapshot_name,
    )


def _start_admin_session(
    cfg: Config,
    conn: sqlite3.Connection,
    binding: models.ClientBinding,
    profile: models.ImageProfile,
    initiator_iqn: str,
    admin_username: str,
    additional_bytes: int = 0,
) -> Session:
    base_path = models.resolve_image_path(cfg.images_root, profile, binding.assigned_version, conn)
    next_version = _next_version_number(conn, profile.id)
    new_path = cfg.images_root / profile.storage_dir / f"base-v{next_version}.img"
    _reflink_or_copy(base_path, new_path)
    _grow_file(new_path, additional_bytes)  # base + 추가 용량 = 총 크기, 전 구간 쓰기 가능(사용자 요청)

    admin_loop_dev = priv_client.attach_loop(cfg.priv_helper_socket, str(new_path), readonly=False)
    lun_name = f"admin-{profile.id}-v{next_version}"

    return Session(
        initiator_iqn=initiator_iqn,
        lun_name=lun_name,
        readonly=False,
        backing_dev=admin_loop_dev,
        base_loop_dev=admin_loop_dev,
        profile_id=profile.id,
        version_number=next_version,
        admin_file_path=new_path,
        admin_username=admin_username,
    )


def start_session(
    cfg: Config,
    conn: sqlite3.Connection,
    binding: models.ClientBinding,
    initiator_iqn: str,
    *,
    readonly: bool = True,
    admin_username: str | None = None,
    additional_bytes: int = 0,
) -> Session:
    """DESIGN.md 3.7 on_client_login과 동일한 로직. 관리자 쓰기 세션(3.5)은
    readonly=False로 동일 함수를 재사용하고, 호출 측에서 단독 점유 락을 추가로 건다.

    additional_bytes: 관리자 세션에서만 의미 있음 — 베이스 디스크에 이 크기만큼
    빈 공간을 더해서(예: 10G 베이스 + 20G 추가 = 30G 전체 쓰기 가능 디스크) 서비스한다.
    """
    profile = models.get_image_profile(conn, binding.image_profile_id)

    if readonly:
        session = _start_client_session(cfg, conn, binding, profile, initiator_iqn)
    else:
        if admin_username is None:
            raise ValueError("관리자 쓰기 세션은 admin_username이 필요합니다")
        session = _start_admin_session(cfg, conn, binding, profile, initiator_iqn, admin_username, additional_bytes)

    lio.register_lun(
        target_wwn=cfg.iscsi_target_wwn,
        initiator_iqn=initiator_iqn,
        lun_name=session.lun_name,
        backing_dev=session.backing_dev,
        portal_ip=cfg.iscsi_portal_ip,
        portal_port=cfg.iscsi_portal_port,
    )

    conn.execute(
        "INSERT OR REPLACE INTO active_session "
        "(initiator_iqn, client_mac, version, readonly, snapshot_name, base_loop_dev, overlay_loop_dev, overlay_path) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            initiator_iqn,
            binding.client_mac,
            session.version_number,
            int(readonly),
            session.snapshot_name,
            session.base_loop_dev,
            session.overlay_loop_dev,
            str(session.overlay_path) if session.overlay_path else None,
        ),
    )
    conn.commit()

    return session


def build_sanboot_script(cfg: Config, conn: sqlite3.Connection, client_mac: str) -> bytes:
    """TFTP가 클라이언트별 동적 sanboot 스크립트를 요청받았을 때 호출(DESIGN.md 3.2/3.3).

    이 시점에 LUN/ACL을 미리 프로비저닝해두고, 그 initiator IQN을 스크립트에
    그대로 실어 보낸다 — iPXE가 로그인할 때와 등록해둔 ACL이 항상 일치한다.
    """
    binding = models.lookup_client_binding_by_mac(conn, client_mac)
    initiator_iqn = initiator_iqn_for_mac(cfg, client_mac)
    session = start_session(cfg, conn, binding, initiator_iqn, readonly=True)

    script = (
        "#!ipxe\n"
        f"set initiator-iqn {initiator_iqn}\n"
        f"sanboot iscsi:{cfg.iscsi_portal_ip}::::{cfg.iscsi_target_wwn}\n"
    )
    return script.encode()


def _session_from_active_row(row: sqlite3.Row) -> Session:
    """리퍼가 DB에 남은 정보만으로 end_session에 넘길 Session을 재구성한다 —
    클라이언트 세션(readonly=1)에서만 쓴다(관리자 세션은 이 경로를 안 탐)."""
    return Session(
        initiator_iqn=row["initiator_iqn"],
        lun_name=row["snapshot_name"],
        readonly=True,
        backing_dev=f"/dev/mapper/{row['snapshot_name']}",
        base_loop_dev=row["base_loop_dev"],
        profile_id=-1,  # 클라이언트 세션 정리엔 필요 없음(merge 없음)
        version_number=row["version"],
        overlay_loop_dev=row["overlay_loop_dev"],
        overlay_path=Path(row["overlay_path"]),
        snapshot_name=row["snapshot_name"],
    )


def reap_disconnected_sessions(cfg: Config, conn: sqlite3.Connection) -> list[str]:
    """iSCSI 로그아웃/전원 종료 등으로 끊어진 클라이언트 세션을 정리한다.

    TFTP 요청 시점(build_sanboot_script)에 loop/snapshot/LUN을 미리 만들어두는데,
    클라이언트가 실제로 접속을 끊어도 그걸 알려주는 이벤트가 없어서 계속 남아있던
    문제를 해결한다. 관리자 세션(readonly=0)은 건드리지 않는다 — 그쪽은 Web UI의
    명시적 완료/취소(lock.py)로만 정리된다.
    """
    rows = conn.execute("SELECT * FROM active_session WHERE readonly = 1").fetchall()
    reaped: list[str] = []
    for row in rows:
        if lio.is_session_active(cfg.iscsi_target_wwn, row["initiator_iqn"]):
            continue
        session = _session_from_active_row(row)
        end_session(cfg, conn, session, merge=False)
        reaped.append(row["initiator_iqn"])
    return reaped


def end_session(cfg: Config, conn: sqlite3.Connection, session: Session, *, merge: bool = False) -> None:
    """세션 종료. merge=True(관리자, 완료)면 복사본을 새 ImageVersion으로 등록하고,
    merge=False면 관리자 세션은 복사본을 폐기, 일반 사용자 세션은 오버레이를 폐기한다.
    """
    lio.remove_lun(cfg.iscsi_target_wwn, session.initiator_iqn, session.lun_name)

    conn.execute("DELETE FROM active_session WHERE initiator_iqn = ?", (session.initiator_iqn,))
    conn.commit()

    if session.readonly:
        priv_client.remove_snapshot(cfg.priv_helper_socket, session.snapshot_name)
        priv_client.detach_loop(cfg.priv_helper_socket, session.overlay_loop_dev)
        priv_client.detach_loop(cfg.priv_helper_socket, session.base_loop_dev)
        session.overlay_path.unlink(missing_ok=True)
        return

    priv_client.detach_loop(cfg.priv_helper_socket, session.base_loop_dev)
    if merge:
        checksum = _sha256_file(session.admin_file_path)
        conn.execute(
            "INSERT INTO image_version (profile_id, version_number, file_path, checksum, created_by) "
            "VALUES (?, ?, ?, ?, ?)",
            (session.profile_id, session.version_number, session.admin_file_path.name, checksum, session.admin_username),
        )
        conn.commit()
    else:
        session.admin_file_path.unlink(missing_ok=True)
