import sqlite3

from diskless import db
from diskless.config import Config, apply_db_overrides


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    db.init_schema(c)
    return c


def test_apply_db_overrides_keeps_default_when_no_setting_stored():
    cfg = Config(cookie_secret="x")
    result = apply_db_overrides(cfg, _conn())
    assert result.dhcp_mode == cfg.dhcp_mode


def test_apply_db_overrides_uses_stored_dhcp_mode():
    conn = _conn()
    conn.execute("INSERT INTO site_setting (key, value) VALUES ('dhcp_mode', 'standalone')")
    conn.commit()

    cfg = Config(cookie_secret="x", dhcp_mode="proxy")
    result = apply_db_overrides(cfg, conn)

    assert result.dhcp_mode == "standalone"
    assert result is not cfg  # frozen dataclass — 새 인스턴스로 반환


def test_apply_db_overrides_does_not_mutate_other_fields():
    conn = _conn()
    conn.execute("INSERT INTO site_setting (key, value) VALUES ('dhcp_mode', 'standalone')")
    conn.commit()

    cfg = Config(cookie_secret="x", web_port=9999)
    result = apply_db_overrides(cfg, conn)

    assert result.web_port == 9999
