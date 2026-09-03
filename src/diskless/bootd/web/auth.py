"""로그인/로그아웃. DESIGN.md 3.5 — bcrypt 해시, 서명된 세션 쿠키(Secure/HttpOnly)."""

from __future__ import annotations

import bcrypt
from twisted.web.iweb import IRequest

from diskless.bootd.web import context
from diskless.bootd.web import render_template, redirect, app
from diskless.webcookie import clear_session_cookie, new_session_payload, set_session_cookie


@app.route("/login", methods=["GET"])
def login_form(request: IRequest) -> bytes:
    return render_template(request, "login.html", session=new_session_payload(""), error=None)


@app.route("/login", methods=["POST"])
def login_submit(request: IRequest) -> bytes:
    username = request.args[b"username"][0].decode()
    password = request.args[b"password"][0]  # bcrypt.checkpw는 bytes를 받으므로 그대로 사용

    row = context.conn.execute(
        "SELECT password_hash FROM admin_user WHERE username = ?", (username,)
    ).fetchone()
    if row is None or not bcrypt.checkpw(password, row["password_hash"].encode()):
        return render_template(
            request, "login.html", session=new_session_payload(""), error="아이디 또는 비밀번호가 올바르지 않습니다"
        )

    set_session_cookie(request, context.cfg.cookie_secret, new_session_payload(username))
    return redirect(request, "/")


@app.route("/logout", methods=["POST"])
def logout(request: IRequest) -> bytes:
    clear_session_cookie(request)
    return redirect(request, "/login")
