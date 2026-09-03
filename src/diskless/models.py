"""ImageProfile / ImageVersion / ClientBinding. DESIGN.md 3.7."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ImageProfile:
    id: int
    name: str
    storage_dir: str
    locked_by: str | None = None
    locked_at: str | None = None


@dataclass(frozen=True)
class ImageVersion:
    id: int
    profile_id: int
    version_number: int
    file_path: str
    checksum: str
    created_at: str
    created_by: str


@dataclass(frozen=True)
class ClientBinding:
    id: int
    client_mac: str
    initiator_iqn: str | None
    image_profile_id: int
    assigned_version: int  # 필수, 자동 default 없음 (DESIGN.md 3.7)


class ClientNotRegisteredError(LookupError):
    pass


def get_image_profile(conn: sqlite3.Connection, profile_id: int) -> ImageProfile:
    row = conn.execute("SELECT * FROM image_profile WHERE id = ?", (profile_id,)).fetchone()
    if row is None:
        raise LookupError(f"unknown image_profile id={profile_id}")
    return ImageProfile(**row)


def lookup_client_binding(conn: sqlite3.Connection, initiator_iqn: str) -> ClientBinding:
    row = conn.execute(
        "SELECT * FROM client_binding WHERE initiator_iqn = ?", (initiator_iqn,)
    ).fetchone()
    if row is None:
        raise ClientNotRegisteredError(initiator_iqn)
    return ClientBinding(**row)


def lookup_client_binding_by_mac(conn: sqlite3.Connection, client_mac: str) -> ClientBinding:
    """TFTP 단계(아직 iSCSI 로그인 전)에서는 MAC만 알고 있으므로 이걸로 먼저 조회한다."""
    row = conn.execute("SELECT * FROM client_binding WHERE client_mac = ?", (client_mac,)).fetchone()
    if row is None:
        raise ClientNotRegisteredError(client_mac)
    return ClientBinding(**row)


def resolve_image_path(images_root: Path, profile: ImageProfile, version_number: int, conn: sqlite3.Connection) -> Path:
    row = conn.execute(
        "SELECT file_path FROM image_version WHERE profile_id = ? AND version_number = ?",
        (profile.id, version_number),
    ).fetchone()
    if row is None:
        raise LookupError(f"profile={profile.name} has no version {version_number}")
    return images_root / profile.storage_dir / row["file_path"]
