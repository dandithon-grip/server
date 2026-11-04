"""Supabase Storage 비공개 버킷 다운로드 래퍼.

F-11 이 기록 사진 바이트를 비전 API 에 넘기기 전에 읽을 때 쓴다.
업로드는 프론트가 사용자 토큰으로 Storage 에 직접 하고, 서버는 image_path 만 받는다.

서버는 service role 키를 런타임에 쓰지 않는다 (11/4 결정, #6 댓글 2번). 요청의
access token 을 그대로 전달해서 버킷 RLS(본인 경로 `{supabase user id}/...` 만 select)가
그대로 적용되게 한다. 그래서 다른 사용자의 사진은 서버를 거쳐도 받을 수 없다.

엔드포인트: GET {SUPABASE_URL}/storage/v1/object/authenticated/{bucket}/{path}
- 헤더 Authorization: Bearer {요청자 access token}, apikey: {anon key}
- 200 이면 응답 본문이 파일 바이트 그대로 (JSON 이 아니다)
- 403 권한 없음 / 404 없는 파일 → IMAGE_NOT_FOUND (404). 비전 실패(502)와 구분한다
문서: https://supabase.com/docs/reference/javascript/storage-from-download
"""

import logging

import httpx

from app.core.config import get_settings
from app.core.exceptions import AppException, ErrorCode

logger = logging.getLogger(__name__)

OBJECT_PATH_PREFIX = "/storage/v1/object/authenticated"
# Storage 가 "권한 없음"과 "없는 파일"에 쓰는 상태코드. 둘 다 사용자에게는 같은 404 다
NOT_FOUND_STATUSES = (403, 404)


def is_safe_object_path(path: str) -> bool:
    """버킷 안의 경로인지 확인한다.

    경로는 요청 본문에서 온 값이라 `..` 이나 선행 `/` 로 버킷 밖을 가리킬 수 있다.
    URL 로 조립하기 전에 막는다.
    """
    if not path or path.startswith("/"):
        return False
    return ".." not in path.split("/")


class SupabaseStorageClient:
    def __init__(
        self,
        supabase_url: str,
        bucket: str,
        *,
        anon_key: str = "",
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """
        :param anon_key: 공개 anon 키. Supabase 게이트웨이가 apikey 헤더를 요구한다.
            비어 있으면 헤더를 붙이지 않는다 (service role 키는 받지 않는다)
        :param transport: 테스트가 가짜 서버를 끼우는 자리. 운영에서는 None (실제 네트워크)
        """
        self._base_url = supabase_url.rstrip("/")
        self._bucket = bucket
        self._anon_key = anon_key
        self._timeout = timeout
        self._transport = transport

    async def download(self, path: str, *, access_token: str) -> bytes:
        """
        비공개 버킷의 객체를 요청자 권한으로 내려받는다.

        :param path: 버킷 안의 객체 경로 (`{supabase user id}/...`). 버킷 이름은 넣지 않는다
        :param access_token: 요청자의 Supabase access token. 그대로 Authorization 에 실린다
        :return: 파일 바이트
        :raises AppException: IMAGE_NOT_FOUND - 버킷 밖 경로, 권한 없음(403), 없는 파일(404)
        :raises AppException: INVALID_TOKEN - 토큰이 비어 있음 (호출하는 쪽의 버그)
        :raises AppException: EXTERNAL_API_FAILED - 네트워크 오류, 그 밖의 4xx/5xx
        """
        # 1. 호출 전 검사. 네트워크를 타지 않고 바로 실패한다
        if not access_token:
            logger.error("Storage 다운로드에 access token 이 없다")
            raise AppException(ErrorCode.INVALID_TOKEN)
        if not is_safe_object_path(path):
            raise AppException(ErrorCode.IMAGE_NOT_FOUND)

        # 2. 요청자 토큰을 그대로 전달한다. 서버의 service role 키는 쓰지 않는다
        headers = {"Authorization": f"Bearer {access_token}"}
        if self._anon_key:
            headers["apikey"] = self._anon_key
        url = f"{self._base_url}{OBJECT_PATH_PREFIX}/{self._bucket}/{path}"

        # 3. 호출. httpx 예외는 여기서 끝낸다
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                response = await client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            logger.error("Storage 다운로드 실패: %s", exc)
            raise AppException(ErrorCode.EXTERNAL_API_FAILED) from exc

        # 4. 상태코드 해석. 경로는 개인정보라 로그에 남기지 않는다
        if response.status_code in NOT_FOUND_STATUSES:
            logger.info("Storage 객체 접근 거절 → %s", response.status_code)
            raise AppException(ErrorCode.IMAGE_NOT_FOUND)
        if response.is_error:
            logger.error("Storage 다운로드 → %s", response.status_code)
            raise AppException(ErrorCode.EXTERNAL_API_FAILED)
        return response.content


def get_storage_client() -> SupabaseStorageClient:
    settings = get_settings()
    return SupabaseStorageClient(
        settings.supabase_url,
        settings.supabase_record_bucket,
        anon_key=settings.supabase_anon_key,
    )
