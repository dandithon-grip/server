from datetime import datetime

from fastapi import APIRouter, Depends, Query

from app.core.security import get_current_user
from app.core.timezone import LOCAL_TZ
from app.domain.commute.models import Commute, Preference
from app.domain.commute.service import CommuteService, get_commute_service
from app.domain.route.fallback import decide_fallback
from app.domain.route.models import Route
from app.domain.route.schemas import RoutesResponse, to_route_response
from app.domain.route.service import RouteService, get_route_service
from app.domain.user.models import User

router = APIRouter(prefix="/commutes/me/routes", tags=["Route"])


def _walk_time(commute: Commute) -> datetime:
    """오늘 퇴근 시각 (데모는 퇴근 카드만, 11/3 결정)."""
    today = datetime.now(LOCAL_TZ).date()
    return datetime.combine(today, commute.leave_time, tzinfo=LOCAL_TZ)


def _ranked_response(
    service: RouteService,
    commute: Commute,
    routes: list[Route],
    preference: Preference,
    tag_weights: dict[str, float],
) -> RoutesResponse:
    at = _walk_time(commute)
    ranked = service.rank(
        routes, preference=preference, at=at, is_clear=None, tag_weights=tag_weights
    )
    decided = decide_fallback(ranked, at=at, is_clear=None)
    return RoutesResponse(
        preference=preference,
        fallback_level=decided.level,
        action_type=decided.action_type,
        candidates=[to_route_response(r) for r in decided.candidates],
        shortest=[to_route_response(r) for r in decided.shortest],
    )


@router.post(
    "",
    response_model=RoutesResponse,
    summary="내 동선의 샛길 생성",
    description=(
        "걷는 구간마다 최단 경로와 경유지 후보 경로를 TMAP 으로 받아 저장한다 (동기, 최대 10초). "
        "기존 경로는 지우고 다시 만든다. 응답은 기본 취향으로 고른 후보 ≤3개 + 최단."
    ),
    responses={
        401: {"description": "토큰 없음 또는 만료"},
        404: {"description": "저장한 동선이 없음"},
        502: {"description": "TMAP 호출 실패"},
    },
)
async def generate_routes(
    current_user: User = Depends(get_current_user),
    commute_service: CommuteService = Depends(get_commute_service),
    route_service: RouteService = Depends(get_route_service),
) -> RoutesResponse:
    commute = await commute_service.get_commute(current_user.id)
    routes = await route_service.generate(commute, current_user.city)
    return _ranked_response(
        route_service, commute, routes, commute.preference, current_user.tag_weights
    )


@router.get(
    "",
    response_model=RoutesResponse,
    summary="내 동선의 샛길 후보 조회",
    description=(
        "저장된 경로에 오늘의 노을 창과 취향을 적용해 후보 ≤3개 + 최단을 돌려준다. "
        "TMAP 을 다시 부르지 않는다. preference 를 주면 그 취향으로 다시 고른다 (취향 칩)."
    ),
    responses={
        401: {"description": "토큰 없음 또는 만료"},
        404: {"description": "저장한 동선이 없음"},
    },
)
async def list_routes(
    preference: Preference | None = Query(None, description="생략하면 동선의 기본 취향"),
    current_user: User = Depends(get_current_user),
    commute_service: CommuteService = Depends(get_commute_service),
    route_service: RouteService = Depends(get_route_service),
) -> RoutesResponse:
    commute = await commute_service.get_commute(current_user.id)
    routes = await route_service.list_routes(commute.id)
    return _ranked_response(
        route_service, commute, routes, preference or commute.preference, current_user.tag_weights
    )
