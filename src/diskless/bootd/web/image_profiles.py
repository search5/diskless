"""이미지 프로파일 CRUD + 버전 이력 + 관리자 쓰기 세션. DESIGN.md 3.5 화면 2·3번."""

from __future__ import annotations

import sqlite3

from twisted.web.iweb import IRequest

from diskless import models
from diskless.bootd.web import app, redirect, render_template, require_auth
from diskless.bootd.web import context
from diskless.orchestration import lock
from diskless.orchestration import session as session_mod
from diskless.webcookie import verify_csrf

# profile_id -> 진행 중인 관리자 쓰기 세션(orchestration.session.Session).
# bootd는 단일 프로세스라 메모리에 두면 충분하고, lock.py의 DB 락과 1:1 대응한다.
_admin_sessions: dict[int, object] = {}


@app.route("/images", methods=["GET"])
@require_auth
def list_profiles(request: IRequest, session: dict) -> bytes:
    rows = context.conn.execute("SELECT * FROM image_profile").fetchall()
    return render_template(request, "image_profiles.html", session=session, profiles=[models.ImageProfile(**r) for r in rows])


@app.route("/images", methods=["POST"])
@require_auth
def create_profile(request: IRequest, session: dict) -> bytes:
    if not verify_csrf(request, session):
        request.setResponseCode(403)
        return b"CSRF token mismatch"

    name = request.args[b"name"][0].decode()
    storage_dir = request.args[b"storage_dir"][0].decode()
    context.conn.execute("INSERT INTO image_profile (name, storage_dir) VALUES (?, ?)", (name, storage_dir))
    context.conn.commit()
    return redirect(request, "/images")


@app.route("/images/<int:profile_id>/delete", methods=["POST"])
@require_auth
def delete_profile(request: IRequest, session: dict, profile_id: int) -> bytes:
    """프로파일 삭제. 이 프로파일을 참조하는 버전/클라이언트 배정이 있으면 FK가
    막아준다(image_version/client_binding 모두 image_profile을 REFERENCES) —
    편집 중(락)이면 그것도 막는다.
    """
    if not verify_csrf(request, session):
        request.setResponseCode(403)
        return b"CSRF token mismatch"

    if lock.is_locked(context.conn, profile_id):
        request.setResponseCode(409)
        return "편집 중인 프로파일은 삭제할 수 없습니다".encode()

    try:
        context.conn.execute("DELETE FROM image_profile WHERE id = ?", (profile_id,))
        context.conn.commit()
    except sqlite3.IntegrityError:
        context.conn.rollback()
        request.setResponseCode(409)
        return "이 프로파일을 참조하는 버전/클라이언트가 있어 삭제할 수 없습니다".encode()

    return redirect(request, "/images")


@app.route("/images/<int:profile_id>/force-unlock", methods=["POST"])
@require_auth
def force_unlock(request: IRequest, session: dict, profile_id: int) -> bytes:
    """고아 락 복구용 수동 해제 — bootd 재시작 시엔 자동으로 풀리지만(6.6),
    프로세스가 안 죽었는데도 락이 안 풀리는 경우(예: 브라우저를 닫고 안 돌아옴)를 위한
    수단이다. 이 프로세스가 그 프로파일의 세션을 메모리에 살아있다고 여기는 동안은
    거부한다 — 그 상태에서 강제로 풀면 다른 관리자가 같은 데이터에 동시에 쓸 수 있게 됨.
    """
    if not verify_csrf(request, session):
        request.setResponseCode(403)
        return b"CSRF token mismatch"

    if profile_id in _admin_sessions:
        request.setResponseCode(409)
        return "이 프로세스에 살아있는 편집 세션이 있어 강제 해제할 수 없습니다 — 완료/취소를 쓰세요".encode()

    lock.release(context.conn, profile_id)
    return redirect(request, f"/images/{profile_id}/versions")


@app.route("/images/<int:profile_id>/versions", methods=["GET"])
@require_auth
def list_versions(request: IRequest, session: dict, profile_id: int) -> bytes:
    rows = context.conn.execute(
        "SELECT * FROM image_version WHERE profile_id = ? ORDER BY version_number DESC", (profile_id,)
    ).fetchall()
    editing = profile_id in _admin_sessions
    return render_template(
        request, "image_versions.html", session=session, profile_id=profile_id,
        versions=[models.ImageVersion(**r) for r in rows],
        editing=editing,
        # 이 프로세스가 모르는 락(고아 락 등)이면 "강제 해제"를 보여줄 수 있게 알려준다
        orphaned_lock=(not editing) and lock.is_locked(context.conn, profile_id),
    )


@app.route("/images/<int:profile_id>/versions/<int:version_number>/delete", methods=["POST"])
@require_auth
def delete_version(request: IRequest, session: dict, profile_id: int, version_number: int) -> bytes:
    """버전 삭제 — 그 버전으로 배정된 클라이언트가 있으면 막는다(dangling 배정 방지).
    client_binding.assigned_version은 FK가 아니라 애플리케이션 값이라 DB가 안 막아준다.
    """
    if not verify_csrf(request, session):
        request.setResponseCode(403)
        return b"CSRF token mismatch"

    in_use = context.conn.execute(
        "SELECT COUNT(*) AS n FROM client_binding WHERE image_profile_id = ? AND assigned_version = ?",
        (profile_id, version_number),
    ).fetchone()["n"]
    if in_use:
        request.setResponseCode(409)
        return f"{in_use}개 클라이언트가 이 버전을 사용 중이라 삭제할 수 없습니다".encode()

    row = context.conn.execute(
        "SELECT * FROM image_version WHERE profile_id = ? AND version_number = ?", (profile_id, version_number)
    ).fetchone()
    if row is None:
        request.setResponseCode(404)
        return b"version not found"

    profile = models.get_image_profile(context.conn, profile_id)
    file_path = context.cfg.images_root / profile.storage_dir / row["file_path"]
    file_path.unlink(missing_ok=True)

    context.conn.execute(
        "DELETE FROM image_version WHERE profile_id = ? AND version_number = ?", (profile_id, version_number)
    )
    context.conn.commit()
    return redirect(request, f"/images/{profile_id}/versions")


@app.route("/images/<int:profile_id>/update-session", methods=["POST"])
@require_auth
def start_update_session(request: IRequest, session: dict, profile_id: int) -> bytes:
    """"이미지 업데이트" 화면 — 관리자 쓰기 세션 시작. DESIGN.md 3.5 화면 3번.

    클라이언트 로그인과 동일한 orchestration.session.start_session을
    readonly=False로 재사용한다(3.5 "오케스트레이션 엔진 재사용"). 단독 점유
    락(lock.py)을 먼저 잡아야만 시작할 수 있다(4장 리스크: 관리자 병합 중 동시성 충돌).
    """
    if not verify_csrf(request, session):
        request.setResponseCode(403)
        return b"CSRF token mismatch"

    username = session["user"]
    base_version = int(request.args[b"base_version"][0])
    additional_gb = int(request.args.get(b"additional_gb", [b"0"])[0] or 0)
    additional_bytes = additional_gb * 1024**3

    try:
        lock.acquire(context.conn, profile_id, username)
    except lock.ProfileLockedError as exc:
        request.setResponseCode(409)
        return str(exc).encode()

    admin_binding = models.ClientBinding(
        id=0, client_mac=f"admin:{username}", initiator_iqn=None,
        image_profile_id=profile_id, assigned_version=base_version,
    )
    initiator_iqn = f"{context.cfg.iscsi_target_iqn_prefix}:admin-ui-{username}"

    try:
        admin_session = session_mod.start_session(
            context.cfg, context.conn, admin_binding, initiator_iqn, readonly=False, admin_username=username,
            additional_bytes=additional_bytes,
        )
    except Exception:
        lock.release(context.conn, profile_id)
        raise

    _admin_sessions[profile_id] = admin_session
    return redirect(request, f"/images/{profile_id}/versions")


@app.route("/images/<int:profile_id>/finish-session", methods=["POST"])
@require_auth
def finish_update_session(request: IRequest, session: dict, profile_id: int) -> bytes:
    """관리자 쓰기 세션 종료. action=merge면 새 ImageVersion으로 등록,
    action=cancel(기본값)이면 복사본을 그냥 폐기한다.
    """
    if not verify_csrf(request, session):
        request.setResponseCode(403)
        return b"CSRF token mismatch"

    admin_session = _admin_sessions.pop(profile_id, None)
    if admin_session is None:
        request.setResponseCode(404)
        return b"no active update session for this profile"

    action = request.args.get(b"action", [b"cancel"])[0].decode()
    try:
        session_mod.end_session(context.cfg, context.conn, admin_session, merge=(action == "merge"))
    finally:
        lock.release(context.conn, profile_id)

    return redirect(request, f"/images/{profile_id}/versions")
