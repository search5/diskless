"""관리자 계정 생성/비밀번호 재설정 CLI. DESIGN.md 3.5.

`admin_user` 테이블은 스키마엔 있지만 첫 계정을 만들 방법이 없었다 — 배포 시
이 커맨드로 최초 관리자 계정을 만든다(같은 username으로 다시 실행하면 비밀번호 갱신).
"""

from __future__ import annotations

import argparse
import getpass
import sqlite3

import bcrypt

from diskless import db
from diskless.config import load_config


def create_admin(conn: sqlite3.Connection, username: str, password: str) -> None:
    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    conn.execute(
        "INSERT INTO admin_user (username, password_hash) VALUES (?, ?) "
        "ON CONFLICT(username) DO UPDATE SET password_hash = excluded.password_hash",
        (username, password_hash),
    )
    conn.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description="Diskless 관리자 계정 생성/비밀번호 재설정")
    parser.add_argument("username")
    parser.add_argument("--password", help="지정 안 하면 프롬프트로 입력받음(터미널에 안 남음)")
    args = parser.parse_args()

    password = args.password or getpass.getpass("비밀번호: ")
    cfg = load_config()
    conn = db.connect(cfg.db_path)
    db.init_schema(conn)
    create_admin(conn, args.username, password)
    print(f"관리자 계정 '{args.username}' 생성/갱신 완료")


if __name__ == "__main__":
    main()
