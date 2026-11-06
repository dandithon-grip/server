from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.core.security import get_access_token, get_current_user
from app.domain.record.schemas import (
    CreateRecordRequest,
    RecordResponse,
    RecordSuggestionResponse,
    SuggestRecordRequest,
)
from app.domain.record.service import RecordService, get_record_service
from app.domain.user.models import User

router = APIRouter(prefix="/records", tags=["Record"])


@router.post(
    "/suggest",
    response_model=RecordSuggestionResponse,
    summary="사진의 계절·단계 제안",
    description=(
        "Storage 에 올린 사진을 요청자 토큰으로 내려받아 비전 AI 가 계절·단계를 제안한다. "
        "저장은 하지 않는다. 장소는 place_id, 없으면 lat·lng 100m 안의 Place 이고 못 찾으면 "
        "null 이다. 비전 실패·스키마 밖·못 읽는 사진 형식은 none 으로 응답한다."
    ),
    responses={
        401: {"description": "토큰 없음 또는 만료"},
        404: {"description": "사진을 찾을 수 없음 (권한 없음 포함) 또는 존재하지 않는 장소"},
        502: {"description": "Storage 호출 실패"},
    },
)
async def suggest_record(
    request: SuggestRecordRequest,
    current_user: User = Depends(get_current_user),
    access_token: str = Depends(get_access_token),
    service: RecordService = Depends(get_record_service),
) -> RecordSuggestionResponse:
    suggestion = await service.suggest(request, city=current_user.city, access_token=access_token)
    return RecordSuggestionResponse.model_validate(suggestion)


@router.post(
    "",
    response_model=RecordResponse,
    status_code=201,
    summary="기록 저장",
    description=(
        "사진을 올린 뒤 고른 계절·단계를 저장한다. 사진은 프론트가 Supabase Storage 에 "
        "직접 올리고 서버는 경로만 받는다. 비전 제안(ai_season·ai_stage)을 함께 보내면 "
        "사용자가 고친 값과 함께 남는다. 둘러보기는 저장할 수 없다."
    ),
    responses={
        401: {"description": "토큰 없음 또는 만료"},
        404: {"description": "존재하지 않는 장소"},
    },
)
async def create_record(
    request: CreateRecordRequest,
    current_user: User = Depends(get_current_user),
    service: RecordService = Depends(get_record_service),
) -> RecordResponse:
    record = await service.create_record(current_user.id, request)
    return RecordResponse.model_validate(record)


@router.get(
    "",
    response_model=list[RecordResponse],
    summary="내 기록 목록",
    description="본인 기록만 최근 날짜부터 돌려준다. place_id 를 주면 그 장소의 기록만.",
    responses={401: {"description": "토큰 없음 또는 만료"}},
)
async def list_records(
    place_id: UUID | None = Query(None, description="장소로 거를 때"),
    current_user: User = Depends(get_current_user),
    service: RecordService = Depends(get_record_service),
) -> list[RecordResponse]:
    records = await service.list_records(current_user.id, place_id)
    return [RecordResponse.model_validate(record) for record in records]
