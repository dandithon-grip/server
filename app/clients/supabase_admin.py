"""Supabase Auth Admin API 래퍼. service role key 가 필요하다.

scripts/seed_demo.py 가 데모 계정을 만들 때만 쓴다.
앱 런타임(app/main.py)은 이 클라이언트를 만들지 않는다.

엔드포인트 (supabase-js auth.admin 이 쓰는 GoTrue API):
- GET  /auth/v1/admin/users?page=&per_page=   → {"users": [...]}
- POST /auth/v1/admin/users                   → 생성된 user 객체
문서: https://supabase.com/docs/reference/javascript/auth-admin-createuser
"""

import logging
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict

from app.core.exceptions import AppException, ErrorCode

logger = logging.getLogger(__name__)

PAGE_SIZE = 100


class AdminUser(BaseModel):
    """Admin API 가 돌려주는 계정 중 서버가 쓰는 필드만."""

    model_config = ConfigDict(frozen=True)

    id: str
    email: str | None = None


class SupabaseAdminClient:
    def __init__(self, supabase_url: str, service_role_key: str, *, timeout: float = 10.0) -> None:
        self._base_url = f"{supabase_url.rstrip('/')}/auth/v1/admin"
        self._headers = {
            "apikey": service_role_key,
            "Authorization": f"Bearer {service_role_key}",
        }
        self._timeout = timeout

    async def find_user_by_email(self, email: str) -> AdminUser | None:
        """계정 목록을 페이지 단위로 훑어 이메일이 같은 계정을 찾는다. 없으면 None."""
        page = 1
        while True:
            body = await self._request(
                "GET", "/users", params={"page": page, "per_page": PAGE_SIZE}
            )
            users: list[dict[str, Any]] = body.get("users", [])
            for raw in users:
                if (raw.get("email") or "").lower() == email.lower():
                    return AdminUser.model_validate(raw)
            if len(users) < PAGE_SIZE:
                return None
            page += 1

    async def create_user(self, *, email: str, password: str, nickname: str) -> AdminUser:
        """이메일 인증이 끝난 상태(email_confirm)로 계정을 만든다."""
        body = await self._request(
            "POST",
            "/users",
            json={
                "email": email,
                "password": password,
                "email_confirm": True,
                "user_metadata": {"nickname": nickname},
            },
        )
        return AdminUser.model_validate(body)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, int] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        :raises AppException: EXTERNAL_API_FAILED - 네트워크 오류 또는 4xx/5xx. 상세는 로그에 남긴다
        """
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url, headers=self._headers, timeout=self._timeout
            ) as client:
                response = await client.request(method, path, params=params, json=json)
                if response.is_error:
                    logger.error(
                        "Supabase Admin API %s %s → %s %s",
                        method,
                        path,
                        response.status_code,
                        response.text,
                    )
                response.raise_for_status()
                result: dict[str, Any] = response.json()
                return result
        except (httpx.HTTPError, ValueError) as exc:
            raise AppException(ErrorCode.EXTERNAL_API_FAILED) from exc
