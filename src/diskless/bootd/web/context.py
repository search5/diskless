"""Klein 라우트 핸들러가 공유하는 런타임 의존성. bootd/main.py가 시작 시 1회 설정한다.

Klein 라우트는 모듈 임포트 시점에 데코레이터로 등록되기 때문에, Tornado의
handler.initialize(**kwargs) 같은 요청별 DI 대신 이 모듈 전역을 쓴다.
"""

from __future__ import annotations

import sqlite3

from diskless.config import Config

cfg: Config
conn: sqlite3.Connection


def configure(app_cfg: Config, app_conn: sqlite3.Connection) -> None:
    global cfg, conn
    cfg = app_cfg
    conn = app_conn
