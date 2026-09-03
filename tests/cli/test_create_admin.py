import bcrypt

from diskless import db
from diskless.cli.create_admin import create_admin


def _conn(tmp_path):
    c = db.connect(tmp_path / "db.sqlite3")
    db.init_schema(c)
    return c


def test_create_admin_inserts_bcrypt_hash(tmp_path):
    conn = _conn(tmp_path)
    create_admin(conn, "admin", "s3cret!")

    row = conn.execute("SELECT * FROM admin_user WHERE username = 'admin'").fetchone()
    assert row is not None
    assert bcrypt.checkpw(b"s3cret!", row["password_hash"].encode())


def test_create_admin_rejects_wrong_password(tmp_path):
    conn = _conn(tmp_path)
    create_admin(conn, "admin", "s3cret!")

    row = conn.execute("SELECT * FROM admin_user WHERE username = 'admin'").fetchone()
    assert not bcrypt.checkpw(b"wrong", row["password_hash"].encode())


def test_create_admin_upserts_existing_username(tmp_path):
    conn = _conn(tmp_path)
    create_admin(conn, "admin", "old-password")
    create_admin(conn, "admin", "new-password")

    rows = conn.execute("SELECT * FROM admin_user WHERE username = 'admin'").fetchall()
    assert len(rows) == 1  # 새 행이 아니라 갱신
    assert bcrypt.checkpw(b"new-password", rows[0]["password_hash"].encode())
    assert not bcrypt.checkpw(b"old-password", rows[0]["password_hash"].encode())
