"""SQLite 연결/스키마. DESIGN.md 3.5/3.7 데이터 모델."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
-- locked_by/locked_at: 관리자 쓰기 세션 단독 점유 락(DESIGN.md 3.5/4장 리스크 —
-- "관리자 병합 중 동시성 충돌" 대응). NULL이면 편집 중이 아님.
CREATE TABLE IF NOT EXISTS image_profile (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    storage_dir TEXT NOT NULL,
    locked_by   TEXT,
    locked_at   TEXT
);

CREATE TABLE IF NOT EXISTS image_version (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id     INTEGER NOT NULL REFERENCES image_profile(id),
    version_number INTEGER NOT NULL,
    file_path      TEXT NOT NULL,
    checksum       TEXT NOT NULL,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    created_by     TEXT NOT NULL,
    UNIQUE (profile_id, version_number)
);

CREATE TABLE IF NOT EXISTS client_binding (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    client_mac       TEXT NOT NULL UNIQUE,
    initiator_iqn    TEXT UNIQUE,
    image_profile_id INTEGER NOT NULL REFERENCES image_profile(id),
    assigned_version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS admin_user (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL
);

-- 세션 생성(TFTP 핸들러, deferToThread 워커 스레드)과 대시보드 폴링(reactor 스레드,
-- LoopingCall)이 서로 다른 스레드에서 같은 커넥션에 접근하므로 이 테이블로 상태를
-- 공유한다 (DESIGN.md 3.5) — connect()의 check_same_thread=False + WAL도 같은 이유.
--
-- snapshot_name/base_loop_dev/overlay_loop_dev/overlay_path: 클라이언트(readonly=1)
-- 세션에서만 채워진다. 세션 리퍼(orchestration/session.py: reap_disconnected_sessions)가
-- 연결이 끊긴 세션을 정리할 때 이 정보로 loop/snapshot을 되찾아 해제한다 —
-- 이 컬럼들이 없으면 initiator_iqn만으로는 뭘 지워야 할지 알 수 없다.
CREATE TABLE IF NOT EXISTS active_session (
    initiator_iqn    TEXT PRIMARY KEY,
    client_mac       TEXT NOT NULL,
    version          INTEGER NOT NULL,
    readonly         INTEGER NOT NULL,
    started_at       TEXT NOT NULL DEFAULT (datetime('now')),
    snapshot_name    TEXT,
    base_loop_dev    TEXT,
    overlay_loop_dev TEXT,
    overlay_path     TEXT
);

-- 사이트별 설정(예: dhcp_mode). bootd는 포트를 systemd 소켓 활성화로 시작 시점에만
-- 넘겨받으므로, 여기 값을 바꿔도 즉시 반영되지 않고 다음 재시작부터 적용된다(3.1/6.5).
CREATE TABLE IF NOT EXISTS site_setting (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # deferToThread로 워커 스레드에서도 이 커넥션을 쓰므로 check_same_thread=False 필요.
    # 동시 쓰기 경합은 이 시스템의 요청 빈도(부팅/관리 세션 단위)상 문제되지 않는 수준.
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
