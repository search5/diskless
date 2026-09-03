"""관리 웹 UI. Klein(Twisted 위 Flask 스타일 라우팅) + Jinja2. DESIGN.md 3.5.

bootd 프로세스 안에서 DHCP/TFTP UDP 리스너와 같은 reactor를 쓰는 HTTP 리소스.
"""

from __future__ import annotations

from functools import wraps

from jinja2 import Environment, FileSystemLoader, select_autoescape
from klein import Klein
from twisted.web.iweb import IRequest

from diskless.bootd.web import context
from diskless.webcookie import read_session

app = Klein()

jinja_env = Environment(
    loader=FileSystemLoader("src/diskless/templates"),
    autoescape=select_autoescape(["html", "xml"]),  # MarkupSafe 기반 자동 이스케이프
)


def render_template(request: IRequest, template_name: str, *, session: dict | None = None, **kwargs: object) -> bytes:
    template = jinja_env.get_template(template_name)
    html = template.render(
        current_user=(session or {}).get("user"),
        xsrf_token=(session or {}).get("csrf", ""),
        **kwargs,
    )
    return html.encode()


def redirect(request: IRequest, location: str) -> bytes:
    request.setResponseCode(302)
    request.setHeader(b"Location", location.encode())
    return b""


def require_auth(handler):
    """세션 쿠키 검증. 없으면 /login으로 리다이렉트하고 핸들러는 호출 안 함."""

    @wraps(handler)
    def wrapper(request: IRequest, *args: object, **kwargs: object):
        session = read_session(request, context.cfg.cookie_secret)
        if session is None:
            return redirect(request, "/login")
        return handler(request, *args, session=session, **kwargs)

    return wrapper
