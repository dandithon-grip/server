from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Response, status

from app.clients.tmap import TmapClient, get_tmap_client
from app.core.security import get_current_user
from app.core.timezone import LOCAL_TZ
from app.domain.commute.models import Commute, Preference, RoutesState
from app.domain.commute.schemas import RideResponse
from app.domain.commute.service import CommuteService, get_commute_service
from app.domain.route.fallback import decide_fallback
from app.domain.route.models import Direction, Route
from app.domain.route.schemas import GenerationStatusResponse, RoutesResponse, to_route_response
from app.domain.route.service import RouteService, generate_in_background, get_route_service
from app.domain.user.models import User

router = APIRouter(prefix="/commutes/me/routes", tags=["Route"])


def _walk_time(commute: Commute, direction: Direction) -> datetime:
    """오늘 걷는 시각. 퇴근이면 퇴근 시각, 출근이면 출근 시각."""
    today = datetime.now(LOCAL_TZ).date()
    clock = commute.leave_time if direction == Direction.TO_HOME else commute.go_time
    return datetime.combine(today, clock, tzinfo=LOCAL_TZ)


async def _ranked_response(
    service: RouteService,
    commute: Commute,
    routes: list[Route],
    preference: Preference,
    tag_weights: dict[str, float],
    direction: Direction,
    city: str,
) -> RoutesResponse:
    at = _walk_time(commute, direction)
    ranked = service.rank(
        routes,
        preference=preference,
        at=at,
        is_clear=None,
        tag_weights=tag_weights,
        direction=direction,
    )
    decided = decide_fallback(ranked, at=at, is_clear=None)
    journeys = await service.journeys(
        commute, decided.candidates, decided.shortest, direction, city
    )
    return RoutesResponse(
        preference=preference,
        direction=direction,
        fallback_level=decided.level,
        action_type=decided.action_type,
        candidates=[to_route_response(r, journeys.get(r.route.id)) for r in decided.candidates],
        shortest=[to_route_response(r, journeys.get(r.route.id)) for r in decided.shortest],
        rides=[RideResponse.model_validate(ride) for ride in commute.rides],
    )


@router.post(
    "",
    response_model=RoutesResponse | GenerationStatusResponse,
    summary="내 동선의 샛길 생성",
    description=(
        "걷는 구간마다 최단 경로와 경유지 후보 경로를 TMAP 으로 받아 저장한다. "
        "기존 경로는 지우고 다시 만든다. 기본은 동기(200, 후보 ≤3개 + 최단). "
        "wait=false 면 백그라운드로 시작하고 202 와 진행 상태를 바로 돌려준다 (#97). "
        "그 뒤 GET /commutes/me/routes/status 로 ready 를 확인한다. "
        "경로는 출근 방향으로 한 번 만들고, direction 에 맞춰 뒤집어 보여준다 (기본 퇴근)."
    ),
    responses={
        202: {
            "description": "백그라운드 생성 시작 (wait=false)",
            "model": GenerationStatusResponse,
        },
        401: {"description": "토큰 없음 또는 만료"},
        404: {"description": "저장한 동선이 없음"},
        502: {"description": "TMAP 호출 실패 (동기일 때)"},
    },
)
async def generate_routes(
    response: Response,
    background: BackgroundTasks,
    direction: Direction = Query(Direction.TO_HOME, description="to_home 퇴근 · to_work 출근"),
    wait: bool = Query(True, description="false 면 백그라운드로 만들고 202 로 바로 답한다"),
    current_user: User = Depends(get_current_user),
    commute_service: CommuteService = Depends(get_commute_service),
    route_service: RouteService = Depends(get_route_service),
    tmap: TmapClient = Depends(get_tmap_client),
) -> RoutesResponse | GenerationStatusResponse:
    commute = await commute_service.get_commute(current_user.id)
    if not wait:
        await commute_service.set_routes_state(commute.id, RoutesState.PENDING)
        background.add_task(generate_in_background, commute.id, current_user.city, tmap)
        response.status_code = status.HTTP_202_ACCEPTED
        return GenerationStatusResponse(
            state=RoutesState.PENDING, updated_at=commute.routes_updated_at, route_count=0
        )
    routes = await route_service.generate(commute, current_user.city)
    await commute_service.set_routes_state(commute.id, RoutesState.READY)
    return await _ranked_response(
        route_service,
        commute,
        routes,
        commute.preference,
        current_user.tag_weights,
        direction,
        current_user.city,
    )


@router.get(
    "/status",
    response_model=GenerationStatusResponse,
    summary="샛길 생성 진행 상태",
    description=(
        "idle(만든 적 없음) · pending(만드는 중) · ready · failed. "
        "pending 이 5분을 넘으면 failed 로 본다 (#97). 프론트가 3초마다 보고 ready 면 경로 조회."
    ),
    responses={401: {"description": "토큰 없음 또는 만료"}, 404: {"description": "동선 없음"}},
)
async def generation_status(
    current_user: User = Depends(get_current_user),
    commute_service: CommuteService = Depends(get_commute_service),
    route_service: RouteService = Depends(get_route_service),
) -> GenerationStatusResponse:
    commute = await commute_service.get_commute(current_user.id)
    routes = await route_service.list_routes(commute.id)
    return GenerationStatusResponse(
        state=commute_service.routes_state_of(commute),
        updated_at=commute.routes_updated_at,
        route_count=len(routes),
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
    direction: Direction = Query(Direction.TO_HOME, description="to_home 퇴근 · to_work 출근"),
    current_user: User = Depends(get_current_user),
    commute_service: CommuteService = Depends(get_commute_service),
    route_service: RouteService = Depends(get_route_service),
) -> RoutesResponse:
    commute = await commute_service.get_commute(current_user.id)
    routes = await route_service.list_routes(commute.id)
    return await _ranked_response(
        route_service,
        commute,
        routes,
        preference or commute.preference,
        current_user.tag_weights,
        direction,
        current_user.city,
    )
