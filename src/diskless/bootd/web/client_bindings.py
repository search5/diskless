"""클라이언트 등록 + 버전 배정. DESIGN.md 3.5 화면 4·6번 — 저장≠배포 원칙의
실제 배포 지점(배정)과, 그 대상이 될 클라이언트를 등록하는 화면.
"""

from __future__ import annotations

import sqlite3

from twisted.web.iweb import IRequest

from diskless import models
from diskless.bootd.web import app, redirect, render_template, require_auth
from diskless.bootd.web import context
from diskless.webcookie import verify_csrf


@app.route("/clients", methods=["GET"])
@require_auth
def list_bindings(request: IRequest, session: dict) -> bytes:
    rows = context.conn.execute("SELECT * FROM client_binding").fetchall()
    profile_rows = context.conn.execute("SELECT * FROM image_profile").fetchall()
    return render_template(
        request, "client_bindings.html", session=session,
        bindings=[models.ClientBinding(**r) for r in rows],
        profiles=[models.ImageProfile(**r) for r in profile_rows],
    )


@app.route("/clients", methods=["POST"])
@require_auth
def register_client(request: IRequest, session: dict) -> bytes:
    """신규 클라이언트(MAC) 등록 — 이미지 프로파일과 최초 배정 버전을 함께 지정한다."""
    if not verify_csrf(request, session):
        request.setResponseCode(403)
        return b"CSRF token mismatch"

    client_mac = request.args[b"client_mac"][0].decode()
    image_profile_id = int(request.args[b"image_profile_id"][0])
    assigned_version = int(request.args[b"assigned_version"][0])

    try:
        context.conn.execute(
            "INSERT INTO client_binding (client_mac, image_profile_id, assigned_version) VALUES (?, ?, ?)",
            (client_mac, image_profile_id, assigned_version),
        )
        context.conn.commit()
    except sqlite3.IntegrityError as exc:
        request.setResponseCode(409)
        return f"등록 실패: {exc}".encode()

    return redirect(request, "/clients")


@app.route("/clients/assign", methods=["POST"])
@require_auth
def assign_version(request: IRequest, session: dict) -> bytes:
    """개별/다중 선택으로 assigned_version 지정. 다음 부팅부터 적용(DESIGN.md 3.7)."""
    if not verify_csrf(request, session):
        request.setResponseCode(403)
        return b"CSRF token mismatch"

    client_ids = [v.decode() for v in request.args.get(b"client_id", [])]
    version_number = int(request.args[b"version_number"][0])
    context.conn.executemany(
        "UPDATE client_binding SET assigned_version = ? WHERE id = ?",
        [(version_number, cid) for cid in client_ids],
    )
    context.conn.commit()
    return redirect(request, "/clients")
