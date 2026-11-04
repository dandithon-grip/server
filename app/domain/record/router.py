from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.core.security import get_current_user
from app.domain.record.schemas import CreateRecordRequest, RecordResponse
from app.domain.record.service import RecordService, get_record_service
from app.domain.user.models import User

router = APIRouter(prefix="/records", tags=["Record"])


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
