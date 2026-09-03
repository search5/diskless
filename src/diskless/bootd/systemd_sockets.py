"""systemd 소켓 활성화로 넘겨받은 fd를 이름으로 조회. DESIGN.md 6.3.

systemd는 LISTEN_FDS부터 fd를 순서대로 넘기고, 각 .socket 유닛에
FileDescriptorName=을 지정하면 LISTEN_FDNAMES로 그 순서/이름을 알려준다.
"""

from __future__ import annotations

import os

FIRST_SYSTEMD_FD = 3


def systemd_fds_by_name() -> dict[str, int]:
    names = os.environ.get("LISTEN_FDNAMES", "")
    if not names:
        return {}
    return {name: FIRST_SYSTEMD_FD + i for i, name in enumerate(names.split(":"))}
