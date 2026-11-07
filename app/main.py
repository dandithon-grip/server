import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings
from app.core.database import engine
from app.core.exceptions import (
    AppException,
    register_exception_handlers,
    use_error_schema_in_openapi,
)
from app.core.security import jwks_cache
from app.domain.card.router import router as card_router
from app.domain.card.service import run_prewarm_forever
from app.domain.commute.router import commute_router, station_router, transit_router
from app.domain.feedback.router import feedback_router, tag_weights_router
from app.domain.notification.router import router as notification_router
from app.domain.record.router import router as record_router
from app.domain.route.router import router as route_router
from app.domain.user.router import router as user_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """앱 시작·종료 훅. yield 앞이 시작, 뒤가 종료다."""
    # 1. Supabase JWKS 선로드. 실패해도 앱은 뜨고, 첫 인증 요청 때 다시 받는다
    try:
        await jwks_cache.load()
    except AppException:
        logger.warning("JWKS 선로드 실패. 첫 인증 요청 때 재시도한다")

    # 2. 운영에서만 퇴근 전 카드 미리 만들기 루프 (#59). 로컬·테스트는 돌리지 않는다
    prewarm = (
        asyncio.create_task(run_prewarm_forever()) if get_settings().app_env == "prod" else None
    )

    yield

    # 3. 루프 정지, 커넥션 풀 반납
    if prewarm is not None:
        prewarm.cancel()
    await engine.dispose()


API_DESCRIPTION = """
출퇴근 중 걷는 구간을 바꿔 자연을 지나가게 하는 **샛길** 의 백엔드 API.

**인증**
- 가입·로그인은 프론트가 Supabase Auth 로 한다. 서버에는 로그인 API 가 없다
- 로그인 후 받은 `access_token` 을 `Authorization: Bearer {token}` 헤더로 보낸다
- 이 화면에서는 오른쪽 위 **Authorize** 에 토큰만 붙여넣는다 (`Bearer` 는 빼고)
- 자물쇠가 없는 API (`GET /cards/demo`, `GET /stations`) 는 둘러보기(게스트)로 호출할 수 있다

**에러**
- 모든 에러는 `{"message": "..."}` 하나다. 메시지는 사용자에게 그대로 보여줘도 된다
- 401 토큰 없음·만료, 404 아직 만들지 않은 데이터, 422 요청 값 오류
- 502 외부 API(TMAP·ODsay·LLM) 실패

**사용 순서**
1. `GET /users/me` 최초 호출 시 사용자 생성
2. `GET /transit/routes` (대중교통만) → `PUT /commutes/me` 동선 저장
3. `POST /commutes/me/routes` 샛길 생성 (TMAP 호출, 수 초)
4. `GET /cards/today` 오늘의 카드 → `POST /feedback` 👍/👎

**타입 생성**: 이 명세의 JSON 은 `/openapi.json` 에 있다.
예: `npx openapi-typescript {서버}/openapi.json -o api.d.ts`
"""

app = FastAPI(title="샛길 API", version="0.1.0", description=API_DESCRIPTION, lifespan=lifespan)
# 프론트(Vercel·localhost)가 브라우저에서 부른다. 허용 origin 은 CORS_ORIGINS 설정
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
register_exception_handlers(app)
use_error_schema_in_openapi(app)
app.include_router(user_router)
app.include_router(commute_router)
app.include_router(station_router)
app.include_router(transit_router)
app.include_router(route_router)
app.include_router(card_router)
app.include_router(feedback_router)
app.include_router(tag_weights_router)
app.include_router(record_router)
app.include_router(notification_router)
