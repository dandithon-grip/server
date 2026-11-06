"""Supabase JWT 검증.

가입·로그인은 Supabase Auth 가 한다 (프론트가 signUp / signInWithPassword 를 호출한다).
서버는 요청의 Authorization: Bearer {access token} 을 Supabase 공개키(JWKS)로 검증만 한다.

흐름: 헤더에서 토큰 → 토큰 헤더의 kid 로 공개키 선택 → 서명·exp·aud 검증 → 클레임 → 사용자 조회·생성
"""

import asyncio
import logging
import time

import httpx
import jwt
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWK, PyJWKSet
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.config import get_settings
from app.core.exceptions import AppException, ErrorCode
from app.domain.user.models import NICKNAME_MAX_LENGTH, User
from app.domain.user.service import UserService, get_user_service

logger = logging.getLogger(__name__)

# Supabase 가 비대칭 키로 서명할 때 쓰는 알고리즘. 공유 비밀키(HS256) 토큰은 받지 않는다
ALLOWED_ALGORITHMS = ("ES256", "RS256")
# kid 불일치로 JWKS 를 다시 받은 뒤 이 시간 안에는 다시 받지 않는다
# (임의 kid 를 넣은 요청으로 Supabase 를 두드리는 것을 막는다)
JWKS_REFETCH_COOLDOWN_SECONDS = 30.0


class SupabaseClaims(BaseModel):
    """검증이 끝난 Supabase access token 의 클레임."""

    model_config = ConfigDict(frozen=True)

    sub: str = Field(..., description="Supabase user id")
    exp: int
    role: str | None = None
    email: str | None = None
    user_metadata: dict[str, object] = Field(default_factory=dict)

    @property
    def nickname(self) -> str | None:
        """signUp 의 options.data.nickname 이 user_metadata.nickname 으로 들어온다.

        없거나 문자열이 아니거나 공백뿐이면 None. 컬럼 길이에 맞춰 자른다.
        """
        value = self.user_metadata.get("nickname")
        if not isinstance(value, str) or not value.strip():
            return None
        return value.strip()[:NICKNAME_MAX_LENGTH]


class JwksCache:
    """Supabase JWKS(공개키 목록) 캐시.

    앱 시작 시 load() 로 한 번 받아 두고, 토큰의 kid 가 캐시에 없을 때만 한 번 더 받는다
    (키 회전 대비).
    """

    def __init__(
        self, jwks_url: str, *, refetch_cooldown: float = JWKS_REFETCH_COOLDOWN_SECONDS
    ) -> None:
        self._jwks_url = jwks_url
        self._refetch_cooldown = refetch_cooldown
        self._keys: dict[str, PyJWK] = {}
        self._last_attempt_at: float | None = None
        self._lock = asyncio.Lock()

    async def load(self) -> None:
        """JWKS 를 받아 캐시를 통째로 교체한다.

        :raises AppException: EXTERNAL_API_FAILED - 네트워크 오류, 비정상 응답, 쓸 수 있는 키 없음
        """
        self._last_attempt_at = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(self._jwks_url)
                response.raise_for_status()
                jwks: dict[str, object] = response.json()
            self.set_keys(jwks)
        except (httpx.HTTPError, ValueError, jwt.PyJWTError) as exc:
            raise AppException(ErrorCode.EXTERNAL_API_FAILED) from exc

    def set_keys(self, jwks: dict[str, object]) -> None:
        """JWKS dict ({"keys": [...]}) 로 캐시를 채운다.

        테스트가 네트워크 없이 키를 넣을 때도 쓴다.
        """
        key_set = PyJWKSet.from_dict(jwks)
        self._keys = {key.key_id: key for key in key_set.keys if key.key_id}
        self._last_attempt_at = time.monotonic()

    async def get_key(self, kid: str) -> PyJWK | None:
        """kid 에 해당하는 공개키. 캐시에 없으면 쿨다운이 지났을 때만 한 번 재조회한다."""
        # 1. 캐시 히트
        if (key := self._keys.get(kid)) is not None:
            return key

        # 2. 캐시 미스 - 동시에 여러 요청이 들어와도 재조회는 한 번만
        async with self._lock:
            if (key := self._keys.get(kid)) is not None:
                return key
            if not self._cooldown_passed():
                return None
            try:
                await self.load()
            except AppException:
                logger.warning("JWKS 재조회 실패: %s", self._jwks_url)
                return None
            return self._keys.get(kid)

    def _cooldown_passed(self) -> bool:
        if self._last_attempt_at is None:
            return True
        return time.monotonic() - self._last_attempt_at >= self._refetch_cooldown


jwks_cache = JwksCache(get_settings().supabase_jwks_url)


def get_jwks_cache() -> JwksCache:
    """의존성으로 감싸 두어 테스트에서 dependency_overrides 로 바꿔치기할 수 있게 한다."""
    return jwks_cache


def decode_token(token: str, key: PyJWK) -> SupabaseClaims:
    """서명·만료(exp)·대상(aud)을 검증하고 클레임을 돌려준다.

    :raises AppException: INVALID_TOKEN - 서명 불일치, 만료, aud 불일치, 필수 클레임 없음
    """
    try:
        payload = jwt.decode(
            token,
            key=key.key,
            algorithms=list(ALLOWED_ALGORITHMS),
            audience=get_settings().supabase_jwt_audience,
            options={"require": ["exp", "sub"]},
        )
        return SupabaseClaims.model_validate(payload)
    except (jwt.PyJWTError, ValidationError) as exc:
        raise AppException(ErrorCode.INVALID_TOKEN) from exc


async def _authenticate(token: str, jwks: JwksCache, user_service: UserService) -> User:
    # 1. 토큰 헤더에서 kid (어느 공개키로 서명했는지)
    try:
        kid = jwt.get_unverified_header(token).get("kid")
    except jwt.PyJWTError as exc:
        raise AppException(ErrorCode.INVALID_TOKEN) from exc
    if not isinstance(kid, str):
        raise AppException(ErrorCode.INVALID_TOKEN)

    # 2. kid 에 맞는 공개키
    key = await jwks.get_key(kid)
    if key is None:
        raise AppException(ErrorCode.INVALID_TOKEN)

    # 3. 서명·exp·aud 검증
    claims = decode_token(token, key)

    # 4. 사용자 조회, 최초 요청이면 생성
    return await user_service.get_or_create_user(supabase_id=claims.sub, nickname=claims.nickname)


# auto_error=False: 헤더가 없을 때 FastAPI 기본 동작(403)을 끄고 아래에서 401 을 직접 던진다.
# Swagger UI 의 Authorize 버튼도 이 객체가 만든다
bearer_scheme = HTTPBearer(auto_error=False, description="Supabase access token")


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    jwks: JwksCache = Depends(get_jwks_cache),
    user_service: UserService = Depends(get_user_service),
) -> User:
    """토큰 필수. 없거나 유효하지 않으면 401."""
    if credentials is None:
        raise AppException(ErrorCode.INVALID_TOKEN)
    return await _authenticate(credentials.credentials, jwks, user_service)


async def get_access_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> str:
    """요청의 access token 원문. Storage 처럼 요청자 권한으로 외부를 부를 때 쓴다.

    서명 검증은 하지 않는다. get_current_user 와 함께 쓴다.
    """
    if credentials is None:
        raise AppException(ErrorCode.INVALID_TOKEN)
    return credentials.credentials


async def get_optional_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    jwks: JwksCache = Depends(get_jwks_cache),
    user_service: UserService = Depends(get_user_service),
) -> User | None:
    """둘러보기(게스트) 허용 엔드포인트용. 토큰이 없으면 None.

    토큰이 있는데 유효하지 않으면 401 이다 - 잘못된 토큰을 게스트로 조용히 넘기지 않는다.
    """
    if credentials is None:
        return None
    return await _authenticate(credentials.credentials, jwks, user_service)
