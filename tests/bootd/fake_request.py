"""Klein 라우트 함수를 실제 HTTP 왕복 없이 직접 호출해 테스트하기 위한 가짜 request.

핸들러들이 실제로 쓰는 IRequest API(`args`, `setResponseCode`, `setHeader`,
`getCookie`, `addCookie`)만 최소로 흉내낸다.
"""

from __future__ import annotations


class FakeRequest:
    def __init__(self, args: dict[bytes, list[bytes]] | None = None, cookies: dict[bytes, bytes] | None = None):
        self.args = args or {}
        self._cookies = cookies or {}
        self.response_code: int | None = None
        self.headers: dict[bytes, bytes] = {}
        self.set_cookies: list[dict] = []

    def setResponseCode(self, code: int) -> None:
        self.response_code = code

    def setHeader(self, name: bytes, value: bytes) -> None:
        self.headers[name] = value

    def getCookie(self, name: bytes) -> bytes | None:
        return self._cookies.get(name)

    def addCookie(self, name, value, **kwargs) -> None:
        self.set_cookies.append({"name": name, "value": value, **kwargs})
