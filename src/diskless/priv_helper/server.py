"""CAP_SYS_ADMIN을 ambient로 보유하는 전용 프로세스. DESIGN.md 6.2.

bootd(Twisted) 프로세스는 capability가 전혀 없고, loop/dm-snapshot 조작은
화이트리스트된 이 RPC를 통해서만 이루어진다. systemd 유닛
(deploy/systemd/diskless-priv-helper.service)이 AmbientCapabilities로
이 프로세스에만 CAP_SYS_ADMIN을 부여한다.

**관리자 쓰기 세션은 dm-snapshot-merge를 쓰지 않는다** — 커널 merge는 원본
디바이스를 그 자리에서 변형하므로, "기존 버전은 그대로 보존"이라는 요구사항과
맞지 않는다. 대신 orchestration/session.py가 파일 복사본(가능하면 reflink)을
만들어 그 복사본을 직접 쓰기 가능한 loop로 마운트한다 — 이 헬퍼는 attach/detach만
하면 되고, snapshot/merge 관련 커널 조작은 필요 없다(화이트리스트가 좁을수록 안전).
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
from pathlib import Path

SOCK_PATH = Path(os.environ.get("DISKLESS_PRIV_SOCK", "/run/diskless/priv-helper.sock"))

DEFAULT_CHUNK_SIZE = 8


def attach_loop(args: dict) -> str:
    cmd = ["losetup", "-f", "--show"]
    if args.get("readonly"):
        cmd.append("-r")
    cmd.append(args["img_path"])
    # ambient capability는 execve()로 넘어가도 유지된다(비-setuid, 파일 capability
    # 없는 일반 바이너리를 exec할 경우) — losetup/dmsetup CLI를 그대로 재사용한다.
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip()


def detach_loop(args: dict) -> None:
    subprocess.run(["losetup", "-d", args["dev"]], check=True)


def _sector_count(dev: str) -> int:
    out = subprocess.run(["blockdev", "--getsz", dev], capture_output=True, text=True, check=True)
    return int(out.stdout.strip())


def create_snapshot(args: dict) -> None:
    """origin_dev는 보통 클라이언트 세션의 base loop device를 그대로 받는다 —
    이 snapshot은 병합하지 않고 세션 종료 시 그냥 버리므로(3.5), origin이
    device-mapper 디바이스일 필요가 없다(raw loop device로 충분)."""
    sectors = _sector_count(args["origin_dev"])
    chunk_size = args.get("chunk_size", DEFAULT_CHUNK_SIZE)
    table = f"0 {sectors} snapshot {args['origin_dev']} {args['overlay_dev']} P {chunk_size}"
    subprocess.run(["dmsetup", "create", args["name"], "--table", table], check=True)


def remove_snapshot(args: dict) -> None:
    subprocess.run(["dmsetup", "remove", args["name"]], check=True)


ALLOWED_OPS = {
    "attach_loop": attach_loop,
    "detach_loop": detach_loop,
    "create_snapshot": create_snapshot,
    "remove_snapshot": remove_snapshot,
}


def _handle(conn: socket.socket) -> None:
    try:
        req = json.loads(conn.recv(65536))
        op = req["op"]
        if op not in ALLOWED_OPS:
            raise ValueError(f"disallowed op: {op}")
        result = ALLOWED_OPS[op](req.get("args", {}))
        conn.sendall(json.dumps({"result": result}).encode())
    except Exception as exc:  # 화이트리스트 밖 요청/실패는 그대로 에러로 회신
        conn.sendall(json.dumps({"error": str(exc)}).encode())
    finally:
        conn.close()


def serve(sock_path: Path = SOCK_PATH) -> None:
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    if sock_path.exists():
        sock_path.unlink()

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sock_path))
    os.chmod(sock_path, 0o660)  # diskless-priv 그룹만 접근 (DESIGN.md 6.2)
    srv.listen(8)

    while True:
        conn, _ = srv.accept()
        _handle(conn)


def main() -> None:
    serve()


if __name__ == "__main__":
    main()
