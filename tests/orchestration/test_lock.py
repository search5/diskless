import sqlite3

import pytest

from diskless import db
from diskless.orchestration import lock


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    db.init_schema(c)
    c.execute("INSERT INTO image_profile (name, storage_dir) VALUES ('win11', 'win11')")
    c.commit()
    return c


def test_acquire_succeeds_when_unlocked(conn):
    lock.acquire(conn, 1, "admin")
    row = conn.execute("SELECT locked_by FROM image_profile WHERE id = 1").fetchone()
    assert row["locked_by"] == "admin"


def test_acquire_fails_when_already_locked_by_someone_else(conn):
    lock.acquire(conn, 1, "admin")
    with pytest.raises(lock.ProfileLockedError) as exc_info:
        lock.acquire(conn, 1, "other-admin")
    assert exc_info.value.locked_by == "admin"


def test_acquire_unknown_profile_raises_lookup_error(conn):
    with pytest.raises(LookupError):
        lock.acquire(conn, 999, "admin")


def test_release_clears_lock_and_allows_reacquire(conn):
    lock.acquire(conn, 1, "admin")
    lock.release(conn, 1)
    lock.acquire(conn, 1, "other-admin")
    row = conn.execute("SELECT locked_by FROM image_profile WHERE id = 1").fetchone()
    assert row["locked_by"] == "other-admin"


def test_is_locked(conn):
    assert lock.is_locked(conn, 1) is False
    lock.acquire(conn, 1, "admin")
    assert lock.is_locked(conn, 1) is True


def test_release_all_returns_empty_when_nothing_locked(conn):
    assert lock.release_all(conn) == []


def test_release_all_clears_locks_and_returns_their_ids(conn):
    conn.execute("INSERT INTO image_profile (name, storage_dir) VALUES ('win10', 'win10')")
    conn.commit()
    lock.acquire(conn, 1, "admin")
    lock.acquire(conn, 2, "other-admin")

    released = lock.release_all(conn)

    assert sorted(released) == [1, 2]
    assert lock.is_locked(conn, 1) is False
    assert lock.is_locked(conn, 2) is False
