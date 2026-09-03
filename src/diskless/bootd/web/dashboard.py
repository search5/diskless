"""대시보드. DESIGN.md 3.5 화면 1번 — 실시간 세션 목록은 websocket.py(autobahn)가 담당."""

from __future__ import annotations

from twisted.web.iweb import IRequest

from diskless.bootd.web import app, render_template, require_auth


@app.route("/", methods=["GET"])
@require_auth
def dashboard(request: IRequest, session: dict) -> bytes:
    return render_template(request, "dashboard.html", session=session)
