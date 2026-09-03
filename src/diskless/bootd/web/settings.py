"""DHCP 운영 모드 설정 화면. DESIGN.md 3.5 화면 5 / 6.5.

bootd는 포트를 systemd 소켓 활성화로 시작 시점에만 넘겨받으므로, 여기서 저장한
값은 **즉시 반영되지 않고 다음 bootd 재시작부터** 적용된다 — 화면에 그 사실을
명시한다(지금 떠 있는 프로세스의 실제 값과 DB에 저장된 값을 둘 다 보여줌).
"""

from __future__ import annotations

from twisted.web.iweb import IRequest

from diskless.bootd.web import app, context, redirect, render_template, require_auth
from diskless.webcookie import verify_csrf

VALID_DHCP_MODES = ("standalone", "proxy")


@app.route("/settings", methods=["GET"])
@require_auth
def show_settings(request: IRequest, session: dict) -> bytes:
    row = context.conn.execute("SELECT value FROM site_setting WHERE key = 'dhcp_mode'").fetchone()
    return render_template(
        request, "settings.html", session=session,
        current_dhcp_mode=context.cfg.dhcp_mode,
        stored_dhcp_mode=row["value"] if row else None,
        valid_modes=VALID_DHCP_MODES,
    )


@app.route("/settings", methods=["POST"])
@require_auth
def update_settings(request: IRequest, session: dict) -> bytes:
    if not verify_csrf(request, session):
        request.setResponseCode(403)
        return b"CSRF token mismatch"

    dhcp_mode = request.args[b"dhcp_mode"][0].decode()
    if dhcp_mode not in VALID_DHCP_MODES:
        request.setResponseCode(400)
        return f"알 수 없는 dhcp_mode: {dhcp_mode!r} ({'/'.join(VALID_DHCP_MODES)} 중 하나)".encode()

    context.conn.execute(
        "INSERT INTO site_setting (key, value) VALUES ('dhcp_mode', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (dhcp_mode,),
    )
    context.conn.commit()
    return redirect(request, "/settings")
