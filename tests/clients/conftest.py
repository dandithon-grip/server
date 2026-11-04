"""clients/ 테스트 공통 도구.

외부 API 래퍼는 전부 httpx 를 쓰므로, 가짜 서버(RecordingTransport)를 여기 한 곳에 둔다.
"""

from collections.abc import Callable

import httpx

Handler = Callable[[httpx.Request], httpx.Response | Exception]


class RecordingTransport(httpx.AsyncBaseTransport):
    """요청을 기록하고 정해진 응답을 돌려주는 가짜 서버 (MockRestServiceServer 에 해당).

    handler 가 Exception 을 돌려주면 그 예외를 던진다 (네트워크 오류 흉내).
    """

    def __init__(self, handler: Handler) -> None:
        self._handler = handler
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self._handler(request)
        if isinstance(response, Exception):
            raise response
        return response
