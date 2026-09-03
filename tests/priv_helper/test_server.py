import subprocess

import pytest

from diskless.priv_helper import server


class FakeCompletedProcess:
    def __init__(self, stdout: str = ""):
        self.stdout = stdout


class FakeRun:
    """subprocess.run 대체품. 호출된 argv를 전부 기록하고, 커맨드별 stdout을 흉내낸다."""

    def __init__(self, stdout_by_prefix: dict[tuple[str, ...], str] | None = None):
        self.calls: list[list[str]] = []
        self.stdout_by_prefix = stdout_by_prefix or {}

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        for prefix, stdout in self.stdout_by_prefix.items():
            if tuple(cmd[: len(prefix)]) == prefix:
                return FakeCompletedProcess(stdout=stdout)
        return FakeCompletedProcess(stdout="")


def test_attach_loop_readonly(monkeypatch):
    fake = FakeRun({("losetup",): "/dev/loop0\n"})
    monkeypatch.setattr(server.subprocess, "run", fake)

    result = server.attach_loop({"img_path": "/images/base.img", "readonly": True})

    assert result == "/dev/loop0"
    assert fake.calls == [["losetup", "-f", "--show", "-r", "/images/base.img"]]


def test_attach_loop_writable_omits_readonly_flag(monkeypatch):
    fake = FakeRun({("losetup",): "/dev/loop1\n"})
    monkeypatch.setattr(server.subprocess, "run", fake)

    server.attach_loop({"img_path": "/images/base.img", "readonly": False})

    assert fake.calls == [["losetup", "-f", "--show", "/images/base.img"]]


def test_detach_loop(monkeypatch):
    fake = FakeRun()
    monkeypatch.setattr(server.subprocess, "run", fake)
    server.detach_loop({"dev": "/dev/loop0"})
    assert fake.calls == [["losetup", "-d", "/dev/loop0"]]


def test_sector_count_queries_blockdev(monkeypatch):
    fake = FakeRun({("blockdev", "--getsz"): "204800\n"})
    monkeypatch.setattr(server.subprocess, "run", fake)

    assert server._sector_count("/dev/loop0") == 204800
    assert fake.calls == [["blockdev", "--getsz", "/dev/loop0"]]


def test_create_snapshot_builds_snapshot_table_from_origin_size(monkeypatch):
    fake = FakeRun({("blockdev", "--getsz"): "204800\n"})
    monkeypatch.setattr(server.subprocess, "run", fake)

    server.create_snapshot({
        "name": "sess1-snap",
        "origin_dev": "/dev/mapper/sess1-origin",
        "overlay_dev": "/dev/loop1",
    })

    assert fake.calls[0] == ["blockdev", "--getsz", "/dev/mapper/sess1-origin"]
    assert fake.calls[1] == [
        "dmsetup", "create", "sess1-snap", "--table",
        "0 204800 snapshot /dev/mapper/sess1-origin /dev/loop1 P 8",
    ]


def test_create_snapshot_accepts_custom_chunk_size(monkeypatch):
    fake = FakeRun({("blockdev", "--getsz"): "1000\n"})
    monkeypatch.setattr(server.subprocess, "run", fake)

    server.create_snapshot({
        "name": "sess1-snap", "origin_dev": "/dev/mapper/o", "overlay_dev": "/dev/loop1", "chunk_size": 32,
    })

    assert fake.calls[1][-1].endswith("P 32")


def test_remove_snapshot(monkeypatch):
    fake = FakeRun()
    monkeypatch.setattr(server.subprocess, "run", fake)
    server.remove_snapshot({"name": "sess1-snap"})
    assert fake.calls == [["dmsetup", "remove", "sess1-snap"]]


def test_allowed_ops_stays_minimal():
    # 화이트리스트는 loop/snapshot의 attach/detach/create/remove만 — 그 이상은
    # 이 프로세스가 특권으로 할 수 있는 일을 넓히므로 의도적으로 최소화한다.
    assert set(server.ALLOWED_OPS) == {"attach_loop", "detach_loop", "create_snapshot", "remove_snapshot"}
