"""출근·퇴근 방향 기대 동작 (#56).

샛길은 출근 방향(집 → 회사) 한 벌만 만든다. 걷는 길은 거꾸로 걸어도 같은 길이라
퇴근 방향으로 볼 때 구간·이름·정류장·좌표·노을 비율만 뒤집는다. 도착 후 구간만 방향마다 따로다.
"""

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from geoalchemy2.shape import from_shape
from httpx import AsyncClient
from shapely.geometry import LineString
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.tmap import get_tmap_client
from app.domain.commute.models import Preference
from app.domain.route.models import Direction, LegType, PlaceType, Route
from app.domain.route.service import RankedRoutes, RouteService
from app.main import app
from tests.conftest import TokenFactory
from tests.domain.route.test_routes import FakeTmap, bearer, place, walk_commute

KST = timezone(timedelta(hours=9))
NOON = datetime(2026, 11, 10, 12, 0, tzinfo=KST)
# 11/10 대구 일몰 17:22. 17:20 은 노을 창 안
SUNSET_EVENING = datetime(2026, 11, 10, 17, 20, tzinfo=KST)
# 저장된(출근 방향) 경로선은 동 → 서. 출근 때 해(서쪽)를 보고 걷는다
EAST = (128.6110, 35.8600)
WEST = (128.6000, 35.8600)


def route(
    leg_type: LegType,
    *,
    is_baseline: bool = False,
    green_pct: float = 0,
    sunset_ratio: float = 0,
    sunset_ratio_reverse: float = 0,
    name: str = "테스트",
    name_reverse: str | None = None,
    alight_station_id: UUID | None = None,
    board_station_id: UUID | None = None,
    direction: Direction = Direction.TO_WORK,
) -> Route:
    return Route(
        id=uuid4(),
        commute_id=uuid4(),
        leg_type=leg_type,
        is_baseline=is_baseline,
        name=name,
        name_reverse=name_reverse,
        minutes=10,
        add_minutes=3,
        polyline=from_shape(LineString([EAST, WEST]), srid=4326),
        green_pct=green_pct,
        water_score=0,
        landmark_score=0,
        sunset_ratio=sunset_ratio,
        sunset_ratio_reverse=sunset_ratio_reverse,
        tags=[],
        spots=[],
        highlights=[],
        alight_station_id=alight_station_id,
        board_station_id=board_station_id,
        skip_stops=1 if leg_type in (LegType.ALIGHT_EARLY, LegType.BOARD_LATE) else 0,
        direction=direction,
    )


def rank(
    routes: list[Route],
    direction: Direction,
    at: datetime = NOON,
    is_clear: bool | None = None,
) -> RankedRoutes:
    service = RouteService.__new__(RouteService)
    return service.rank(
        routes, preference=Preference.FULL, at=at, is_clear=is_clear, direction=direction
    )


BASELINES = [
    route(LegType.FRONT_WALK, is_baseline=True),
    route(LegType.BACK_WALK, is_baseline=True),
]


def test_to_home_mirrors_alight_early_near_work_into_board_late() -> None:
    # 출근 때 회사 근처 "먼저 내리기" 는 퇴근 때 회사 근처 "걸어가서 타기" 다
    station = uuid4()
    early = route(
        LegType.ALIGHT_EARLY,
        green_pct=40,
        name="삼덕교회에서 내려 걷기",
        name_reverse="삼덕교회까지 걸어가서 타기",
        alight_station_id=station,
    )

    to_work = rank([*BASELINES, early], Direction.TO_WORK).candidates[0]
    to_home = rank([*BASELINES, early], Direction.TO_HOME).candidates[0]

    assert (to_work.leg_type, to_work.name, to_work.alight_station_id) == (
        LegType.ALIGHT_EARLY,
        "삼덕교회에서 내려 걷기",
        station,
    )
    assert (to_home.leg_type, to_home.name) == (LegType.BOARD_LATE, "삼덕교회까지 걸어가서 타기")
    assert (to_home.board_station_id, to_home.alight_station_id) == (station, None)
    # 지도 좌표도 거꾸로 (퇴근은 회사에서 출발)
    assert to_work.points[0] == (EAST[1], EAST[0])
    assert to_home.points[0] == (WEST[1], WEST[0])


def test_to_home_swaps_front_and_back_walk() -> None:
    shortest = rank(BASELINES, Direction.TO_HOME).shortest

    assert [r.leg_type for r in shortest] == [LegType.BACK_WALK, LegType.FRONT_WALK]


def test_sunset_uses_the_ratio_of_the_walking_direction() -> None:
    # 출근 방향으로는 해를 보고 걷고(1.0), 퇴근 방향으로는 등지고 걷는(0.0) 길
    west_facing = route(LegType.BACK_WALK, sunset_ratio=1.0, sunset_ratio_reverse=0.0)
    routes = [*BASELINES, west_facing]

    to_work = rank(routes, Direction.TO_WORK, at=SUNSET_EVENING, is_clear=True)
    to_home = rank(routes, Direction.TO_HOME, at=SUNSET_EVENING, is_clear=True)

    assert [r.event_score for r in to_work.eligible] == [10.0]
    assert to_home.eligible == []


def test_after_arrival_is_kept_only_for_its_own_direction() -> None:
    near_work = route(LegType.AFTER_ARRIVAL, green_pct=40, direction=Direction.TO_WORK)
    near_home = route(LegType.AFTER_ARRIVAL, green_pct=40, direction=Direction.TO_HOME)

    to_home = rank([*BASELINES, near_work, near_home], Direction.TO_HOME)

    assert [r.route.id for r in to_home.eligible] == [near_home.id]


# --- API ---------------------------------------------------------------------


async def test_generate_makes_after_arrival_on_both_sides(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    db_session.add_all(
        [
            place("집앞공원", 35.8603, 128.6003, [PlaceType.PARK]),
            place("회사앞공원", 35.8603, 128.6107, [PlaceType.PARK]),
        ]
    )
    await db_session.flush()
    app.dependency_overrides[get_tmap_client] = lambda: FakeTmap()
    headers = bearer(make_token())
    await client.put("/commutes/me", json=walk_commute(), headers=headers)

    await client.post("/commutes/me/routes", headers=headers)

    rows = (
        await db_session.execute(select(Route).where(Route.leg_type == LegType.AFTER_ARRIVAL))
    ).scalars()
    sides = {(r.direction, r.name) for r in rows}
    assert ("to_work", "회사앞공원 샛길 (도착 후)") in sides
    assert ("to_home", "집앞공원 샛길 (도착 후)") in sides


async def test_routes_api_defaults_to_home_and_accepts_to_work(
    client: AsyncClient, make_token: TokenFactory
) -> None:
    app.dependency_overrides[get_tmap_client] = lambda: FakeTmap()
    headers = bearer(make_token())
    await client.put("/commutes/me", json=walk_commute(), headers=headers)
    await client.post("/commutes/me/routes", headers=headers)

    home = await client.get("/commutes/me/routes", headers=headers)
    work = await client.get("/commutes/me/routes", params={"direction": "to_work"}, headers=headers)

    assert home.json()["direction"] == "to_home"
    assert work.json()["direction"] == "to_work"
    # 도보 동선: 출근은 집(A)에서, 퇴근은 회사(B)에서 출발한다
    commute = walk_commute()
    assert work.json()["shortest"][0]["polyline"][0]["lng"] == commute["from_lng"]
    assert home.json()["shortest"][0]["polyline"][0]["lng"] == commute["to_lng"]
