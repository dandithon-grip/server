import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings
from app.core.database import engine
from app.core.exceptions import AppException, register_exception_handlers
from app.core.security import jwks_cache
from app.domain.card.router import router as card_router
from app.domain.commute.router import commute_router, station_router, transit_router
from app.domain.feedback.router import feedback_router, tag_weights_router
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

    yield

    # 2. 커넥션 풀 반납
    await engine.dispose()


app = FastAPI(title="샛길 API", version="0.1.0", lifespan=lifespan)
# 프론트(Vercel·localhost)가 브라우저에서 부른다. 허용 origin 은 CORS_ORIGINS 설정
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
register_exception_handlers(app)
app.include_router(user_router)
app.include_router(commute_router)
app.include_router(station_router)
app.include_router(transit_router)
app.include_router(route_router)
app.include_router(card_router)
app.include_router(feedback_router)
app.include_router(tag_weights_router)
app.include_router(record_router)
