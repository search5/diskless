"""priv_helper.py 소켓 클라이언트. Tornado 메인 앱(일반 유저)에서만 사용. DESIGN.md 6.2."""

from __future__ import annotations

import json
import socket
from pathlib import Path


class PrivHelperError(RuntimeError):
    pass


def call_priv_helper(sock_path: Path, op: str, **args: object) -> object:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(str(sock_path))
        s.sendall(json.dumps({"op": op, "args": args}).encode())
        resp = json.loads(s.recv(65536))
    if "error" in resp:
        raise PrivHelperError(resp["error"])
    return resp["result"]


def attach_loop(sock_path: Path, img_path: str, readonly: bool = True) -> str:
    return call_priv_helper(sock_path, "attach_loop", img_path=img_path, readonly=readonly)  # type: ignore[return-value]


def detach_loop(sock_path: Path, dev: str) -> None:
    call_priv_helper(sock_path, "detach_loop", dev=dev)


def create_snapshot(sock_path: Path, name: str, origin_dev: str, overlay_dev: str, chunk_size: int = 8) -> None:
    call_priv_helper(
        sock_path, "create_snapshot", name=name, origin_dev=origin_dev, overlay_dev=overlay_dev, chunk_size=chunk_size
    )


def remove_snapshot(sock_path: Path, name: str) -> None:
    call_priv_helper(sock_path, "remove_snapshot", name=name)
