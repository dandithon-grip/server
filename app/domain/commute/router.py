from fastapi import APIRouter, Depends, Query

from app.core.config import get_settings
from app.core.security import get_current_user
from app.domain.commute.schemas import CommuteResponse, SaveCommuteRequest, StationResponse
from app.domain.commute.service import CommuteService, get_commute_service
from app.domain.user.models import User

commute_router = APIRouter(prefix="/commutes", tags=["Commute"])
station_router = APIRouter(prefix="/stations", tags=["Station"])


@commute_router.put(
    "/me",
    response_model=CommuteResponse,
    summary="내 출퇴근 동선 저장",
    description=(
        "출발·도착 좌표와 동 이름, 출퇴근 시각, 교통수단, 정류장, 기본 취향을 저장한다. "
        "사용자당 1개이며 다시 호출하면 덮어쓴다. walk/car 는 정류장을 무시한다."
    ),
    responses={
        400: {"description": "transit 인데 정류장이 없거나 모르는 정류장"},
        401: {"description": "토큰 없음 또는 만료"},
    },
)
async def save_commute(
    request: SaveCommuteRequest,
    current_user: User = Depends(get_current_user),
    service: CommuteService = Depends(get_commute_service),
) -> CommuteResponse:
    commute = await service.save_commute(current_user.id, request)
    return CommuteResponse.model_validate(commute)


@commute_router.get(
    "/me",
    response_model=CommuteResponse,
    summary="내 출퇴근 동선 조회",
    responses={
        401: {"description": "토큰 없음 또는 만료"},
        404: {"description": "아직 저장한 동선이 없음"},
    },
)
async def get_commute(
    current_user: User = Depends(get_current_user),
    service: CommuteService = Depends(get_commute_service),
) -> CommuteResponse:
    commute = await service.get_commute(current_user.id)
    return CommuteResponse.model_validate(commute)


@station_router.get(
    "",
    response_model=list[StationResponse],
    summary="노선 정류장 목록",
    description="노선의 정류장을 순서대로 돌려준다. 둘러보기(토큰 없음)도 호출할 수 있다.",
)
async def list_stations(
    line: str = Query(..., min_length=1, max_length=10, description="노선 (예: 2)"),
    city: str | None = Query(None, max_length=20, description="도시 코드. 생략하면 기본 도시"),
    service: CommuteService = Depends(get_commute_service),
) -> list[StationResponse]:
    stations = await service.list_stations(line=line, city=city or get_settings().default_city)
    return [StationResponse.model_validate(station) for station in stations]
