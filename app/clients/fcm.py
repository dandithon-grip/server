"""FCM 웹 푸시 클라이언트 (#63).

Firebase Admin SDK 없이 HTTP 로 보낸다. 형식은 공식 문서를 따른다.
- 발송: firebase.google.com/docs/cloud-messaging/send/v1-api
- 인증: developers.google.com/identity/protocols/oauth2/service-account (JWT bearer)

서비스 계정 JWT(RS256) → OAuth 액세스 토큰(최대 1시간, 만료 1분 전까지 재사용) → messages:send.
문서상 HTTP 404 는 UNREGISTERED(토큰 무효)다. 실패 응답 본문 구조는 문서에 없어서 해석하지 않는다.
"""

import json
import logging
import time
from typing import Any

import httpx
import jwt

from app.core.config import get_settings
from app.core.exceptions import AppException, ErrorCode

logger = logging.getLogger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"
SEND_URL = "https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"
FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
GRANT_TYPE = "urn:ietf:params:oauth:grant-type:jwt-bearer"
ASSERTION_LIFETIME_SECONDS = 3600  # 문서: exp 는 iat 뒤 최대 1시간
TOKEN_REFRESH_MARGIN_SECONDS = 60


class FcmClient:
    def __init__(
        self,
        service_account_json: str,
        *,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """
        :param service_account_json: Firebase 서비스 계정 키 JSON 문자열. 비어 있으면 미설정
        """
        self._account: dict[str, Any] = (
            json.loads(service_account_json) if service_account_json else {}
        )
        self._timeout = timeout
        self._transport = transport
        self._access_token: str | None = None
        self._expires_at = 0.0

    @property
    def configured(self) -> bool:
        return bool(self._account)

    async def send(
        self, token: str, *, title: str, body: str, link: str | None, icon: str | None = None
    ) -> bool:
        """
        브라우저 하나에 웹 푸시 1건.

        :param token: 프론트가 Firebase getToken 으로 받은 등록 토큰
        :param link: 알림을 누르면 열 주소. https 가 아니면 넣지 않는다 (FCM 이 https 만 받음)
        :param icon: 알림 아이콘 주소. 없으면 브라우저가 사이트 첫 글자를 보여준다 (#110). https 만
        :return: 보냈으면 True, 토큰이 무효(404 UNREGISTERED)면 False
        :raises AppException: EXTERNAL_API_FAILED - 미설정·인증 실패·네트워크·그 밖의 4xx/5xx
        """
        if not self.configured:
            raise AppException(ErrorCode.EXTERNAL_API_FAILED)

        # 1. 메시지
        webpush: dict[str, Any] = {"notification": {"title": title, "body": body}}
        if icon and icon.startswith("https://"):
            webpush["notification"]["icon"] = icon
        if link and link.startswith("https://"):
            webpush["fcm_options"] = {"link": link}
        payload = {"message": {"token": token, "webpush": webpush}}

        # 2. 발송
        url = SEND_URL.format(project_id=self._account["project_id"])
        headers = {"Authorization": f"Bearer {await self._token()}"}
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                response = await client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise AppException(ErrorCode.EXTERNAL_API_FAILED) from exc

        # 3. 404 는 토큰 무효, 그 밖의 실패는 외부 API 실패
        if response.status_code == httpx.codes.NOT_FOUND:
            return False
        if response.is_error:
            logger.error("FCM 발송 실패 %s", response.status_code)
            raise AppException(ErrorCode.EXTERNAL_API_FAILED)
        return True

    async def _token(self) -> str:
        """OAuth 액세스 토큰. 만료 1분 전까지 재사용한다."""
        now = time.time()
        if self._access_token and now < self._expires_at - TOKEN_REFRESH_MARGIN_SECONDS:
            return self._access_token

        # 1. 서비스 계정 JWT (RS256)
        claims = {
            "iss": self._account["client_email"],
            "scope": FCM_SCOPE,
            "aud": TOKEN_URL,
            "iat": int(now),
            "exp": int(now) + ASSERTION_LIFETIME_SECONDS,
        }
        assertion = jwt.encode(
            claims,
            self._account["private_key"],
            algorithm="RS256",
            headers={"kid": self._account.get("private_key_id", "")},
        )

        # 2. 토큰 교환
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                response = await client.post(
                    TOKEN_URL, data={"grant_type": GRANT_TYPE, "assertion": assertion}
                )
                response.raise_for_status()
                body: dict[str, Any] = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.error("FCM 액세스 토큰 발급 실패")
            raise AppException(ErrorCode.EXTERNAL_API_FAILED) from exc

        self._access_token = str(body["access_token"])
        self._expires_at = now + float(body["expires_in"])
        return self._access_token


_fcm_client: FcmClient | None = None


def get_fcm_client() -> FcmClient:
    """액세스 토큰을 재사용하려고 프로세스에 하나만 만든다."""
    global _fcm_client
    if _fcm_client is None:
        _fcm_client = FcmClient(get_settings().fcm_service_account_json)
    return _fcm_client
