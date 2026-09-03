"""이미지 프로파일 단독 점유 락. DESIGN.md 3.5/4장 리스크(관리자 병합 중 동시성 충돌).

관리자 쓰기 세션은 이 락을 잡은 동안에만 진행되고, 세션 종료(완료/취소) 시 해제한다.
"""

from __future__ import annotations

import sqlite3


class ProfileLockedError(RuntimeError):
    def __init__(self, profile_id: int, locked_by: str) -> None:
        super().__init__(f"프로파일 {profile_id}은(는) 이미 {locked_by}가 편집 중입니다")
        self.profile_id = profile_id
        self.locked_by = locked_by


def acquire(conn: sqlite3.Connection, profile_id: int, username: str) -> None:
    cur = conn.execute(
        "UPDATE image_profile SET locked_by = ?, locked_at = datetime('now') WHERE id = ? AND locked_by IS NULL",
        (username, profile_id),
    )
    conn.commit()
    if cur.rowcount == 0:
        row = conn.execute("SELECT locked_by FROM image_profile WHERE id = ?", (profile_id,)).fetchone()
        if row is None:
            raise LookupError(f"unknown image_profile id={profile_id}")
        raise ProfileLockedError(profile_id, row["locked_by"])


def release(conn: sqlite3.Connection, profile_id: int) -> None:
    conn.execute("UPDATE image_profile SET locked_by = NULL, locked_at = NULL WHERE id = ?", (profile_id,))
    conn.commit()


def is_locked(conn: sqlite3.Connection, profile_id: int) -> bool:
    row = conn.execute("SELECT locked_by FROM image_profile WHERE id = ?", (profile_id,)).fetchone()
    return row is not None and row["locked_by"] is not None


def release_all(conn: sqlite3.Connection) -> list[int]:
    """모든 락을 해제하고 해제된 프로파일 id를 반환한다. bootd 시작 시 호출한다 —
    이 시점엔 (막 시작한 프로세스라) 살아있는 관리자 세션이 있을 수 없으므로, DB에
    남아있는 락은 전부 이전 프로세스가 비정상 종료하며 못 푼 고아 락이다.
    """
    rows = conn.execute("SELECT id FROM image_profile WHERE locked_by IS NOT NULL").fetchall()
    ids = [row["id"] for row in rows]
    if ids:
        conn.execute("UPDATE image_profile SET locked_by = NULL, locked_at = NULL WHERE locked_by IS NOT NULL")
        conn.commit()
    return ids
