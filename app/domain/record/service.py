"""F-11 기록 남기기.

사진 업로드와 비전 호출은 이 service 가 하지 않는다.
프론트가 Supabase Storage 에 올린 경로와, 비전 제안을 사용자가 고친 결과를 받아 저장한다.
"""

import datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.exceptions import AppException, ErrorCode
from app.domain.record.models import Record
from app.domain.record.schemas import CreateRecordRequest
from app.domain.route.service import RouteService, get_route_service

# 기록 날짜를 생략했을 때 쓰는 "오늘"의 기준 시간대
LOCAL_TZ = ZoneInfo("Asia/Seoul")


class RecordService:
    def __init__(self, session: AsyncSession, route_service: RouteService) -> None:
        self._session = session
        self._route_service = route_service

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
) -> RecordService:
    return RecordService(session, route_service)
