"""서명된 세션 쿠키 + CSRF 토큰. DESIGN.md 3.5.

Klein/twisted.web에는 Tornado의 secure_cookie/xsrf_cookies 같은 내장 기능이
없어서 itsdangerous로 직접 구현한다.
"""

from __future__ import annotations

import secrets

from itsdangerous import BadSignature, URLSafeTimedSerializer
from twisted.web.iweb import IRequest

SESSION_COOKIE = "diskless_session"
SESSION_MAX_AGE_SECONDS = 8 * 3600


def _serializer(cookie_secret: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(cookie_secret, salt="diskless-session")


def new_session_payload(username: str) -> dict:
    return {"user": username, "csrf": secrets.token_urlsafe(32)}


def set_session_cookie(request: IRequest, cookie_secret: str, payload: dict) -> None:
    token = _serializer(cookie_secret).dumps(payload)
    request.addCookie(SESSION_COOKIE, token, path="/", httpOnly=True, secure=True, sameSite="lax")


def clear_session_cookie(request: IRequest) -> None:
    request.addCookie(SESSION_COOKIE, "", path="/", httpOnly=True, secure=True, expires=b"Thu, 01 Jan 1970 00:00:00 GMT")


def read_session(request: IRequest, cookie_secret: str) -> dict | None:
    raw = request.getCookie(SESSION_COOKIE.encode())
    if raw is None:
        return None
    try:
        return _serializer(cookie_secret).loads(raw.decode(), max_age=SESSION_MAX_AGE_SECONDS)
    except BadSignature:
        return None


def verify_csrf(request: IRequest, session: dict) -> bool:
    submitted = request.args.get(b"_csrf", [b""])[0].decode()
    return secrets.compare_digest(submitted, session.get("csrf", ""))
