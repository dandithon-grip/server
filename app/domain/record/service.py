"""F-11 기록 남기기.

사진 업로드는 프론트가 Supabase Storage 에 직접 한다. 서버는 경로만 받는다.
suggest: 경로 → Storage 다운로드 1회(요청자 토큰) → 비전 1회 → 계절·단계 제안
create_record: 제안을 사용자가 고친 결과를 저장
"""

import datetime
import logging
from dataclasses import dataclass
from uuid import UUID

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.llm import ImageMediaType, LlmClient, get_llm_client
from app.clients.supabase_storage import SupabaseStorageClient, get_storage_client
from app.core.database import get_db
from app.core.exceptions import AppException, ErrorCode
from app.core.timezone import LOCAL_TZ
from app.domain.record.models import Record, Season, Stage
from app.domain.record.schemas import CreateRecordRequest, SuggestRecordRequest, VisionTags
from app.domain.route.service import RouteService, get_route_service

logger = logging.getLogger(__name__)

PLACE_RADIUS_M = 100  # 현재 위치에서 장소를 자동으로 고르는 반경 (F-11)
VISION_PROMPT = """이 사진의 계절 풍경을 아래 두 값으로 분류한다.
- season: cherry 벚꽃 / foliage 단풍 / ginkgo 은행 / snow 눈 / sunset 노을 / none 해당 없음
- stage: peak 절정 / mid 중간 / end 끝물 / none 해당 없음
사진에서 확실히 보이는 것만 고른다. 확실하지 않으면 none 이다. 퍼센트는 출력하지 않는다."""


@dataclass(frozen=True)
class RecordSuggestion:
    season: Season
    stage: Stage
    place_id: UUID | None


def detect_image_media_type(content: bytes) -> ImageMediaType | None:
    """
    파일 앞부분의 시그니처로 형식을 판별한다. 비전이 못 읽는 형식이면 None.

    경로의 확장자는 사용자가 정한 값이라 믿지 않는다.
    """
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    return None


class RecordService:
    def __init__(
        self,
        session: AsyncSession,
        route_service: RouteService,
        storage: SupabaseStorageClient,
        llm: LlmClient,
    ) -> None:
        self._session = session
        self._route_service = route_service
        self._storage = storage
        self._llm = llm

    async def suggest(
        self, request: SuggestRecordRequest, *, city: str, access_token: str
    ) -> RecordSuggestion:
        """
        사진의 계절·단계를 비전으로 제안하고 장소를 정한다. 저장은 하지 않는다.

        :param request: 사진 경로와 장소 또는 현재 위치
        :param city: 요청자의 도시. 현재 위치로 장소를 찾을 때 쓴다
        :param access_token: 요청자의 Supabase access token. Storage RLS 가 이 권한으로 적용된다
        :return: 제안. 비전 실패·스키마 밖·못 읽는 형식이면 season·stage 는 none
        :raises AppException: PLACE_NOT_FOUND - 모르는 장소 id
        :raises AppException: IMAGE_NOT_FOUND - 권한 없음 또는 없는 사진
        :raises AppException: EXTERNAL_API_FAILED - Storage 장애
        """
        # 1. 장소. 비전 비용을 쓰기 전에 잘못된 요청을 거른다
        place_id = await self._resolve_place_id(request, city)

        # 2. 사진 다운로드. Storage 실패는 비전 실패와 달리 그대로 올린다
        content = await self._storage.download(request.image_path, access_token=access_token)

        # 3. 비전. 좌표는 프롬프트에 넣지 않는다
        tags = await self._vision_tags(content)
        return RecordSuggestion(season=tags.season, stage=tags.stage, place_id=place_id)

    async def _resolve_place_id(self, request: SuggestRecordRequest, city: str) -> UUID | None:
        if request.place_id is not None:
            if await self._route_service.get_place(request.place_id) is None:
                raise AppException(ErrorCode.PLACE_NOT_FOUND)
            return request.place_id
        if request.lat is None or request.lng is None:
            return None
        place = await self._route_service.find_nearest_place(
            request.lat, request.lng, city=city, radius_m=PLACE_RADIUS_M
        )
        return place.id if place is not None else None

    async def _vision_tags(self, content: bytes) -> VisionTags:
        """비전 제안. 어떤 실패든 none 으로 돌려준다 - 사용자가 직접 고르면 된다 (F-11 예외)."""
        fallback = VisionTags(season=Season.NONE, stage=Stage.NONE)
        media_type = detect_image_media_type(content)
        if media_type is None:
            logger.warning("비전이 읽을 수 없는 사진 형식이다")
            return fallback
        try:
            return await self._llm.vision_json(content, media_type, VISION_PROMPT, VisionTags)
        except AppException as exc:
            logger.warning("비전 실패, none 으로 폴백: %s", exc.code.name)
            return fallback

    async def create_record(self, user_id: UUID, request: CreateRecordRequest) -> Record:
        """
        기록을 저장한다. 사용자가 고친 값과 비전 제안을 둘 다 남긴다.

        :param user_id: 저장할 사용자 (users.id)
        :param request: 검증이 끝난 요청. 장소·계절·단계·비전 제안·사진 경로
        :return: 저장된 Record
        :raises AppException: PLACE_NOT_FOUND - 모르는 장소 id
        """
        # 1. 장소 확인. 모르는 id 를 FK 위반(500)으로 흘리지 않고 404 로 돌려준다
        if await self._route_service.get_place(request.place_id) is None:
            raise AppException(ErrorCode.PLACE_NOT_FOUND)

        # 2. 저장. season 은 사용자의 최종 선택, ai_season 은 비전이 제안한 값이다.
        #    사용자가 고치면 두 값이 달라지고, 그 차이가 F-14 집계의 재료가 된다
        record = Record(
            user_id=user_id,
            place_id=request.place_id,
            date=request.date or datetime.datetime.now(LOCAL_TZ).date(),
            season=request.season,
            stage=request.stage,
            ai_season=request.ai_season,
            ai_stage=request.ai_stage,
            title=request.title,
            image_path=request.image_path,
        )
        self._session.add(record)

        # 3. commit
        await self._session.commit()
        return record

    async def list_records(self, user_id: UUID, place_id: UUID | None = None) -> list[Record]:
        """
        본인 기록을 최근 날짜부터 돌려준다.

        :param user_id: 조회할 사용자 (users.id). 다른 사용자의 기록은 섞이지 않는다
        :param place_id: 주면 그 장소의 기록만 (모아보기 F-12)
        """
        # 1. 조회 조건. user_id 는 토큰에서 온 값이라 요청으로 바꿀 수 없다
        stmt = select(Record).where(Record.user_id == user_id)
        if place_id is not None:
            stmt = stmt.where(Record.place_id == place_id)
        stmt = stmt.order_by(Record.date.desc(), Record.created_at.desc())

        # 2. 결과 해석
        return list((await self._session.execute(stmt)).scalars())


def get_record_service(
    session: AsyncSession = Depends(get_db),
    route_service: RouteService = Depends(get_route_service),
    storage: SupabaseStorageClient = Depends(get_storage_client),
    llm: LlmClient = Depends(get_llm_client),
) -> RecordService:
    return RecordService(session, route_service, storage, llm)
