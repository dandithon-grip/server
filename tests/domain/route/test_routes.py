"""F-03 샛길 생성 기대 동작.

TMAP 은 직선 경로를 돌려주는 가짜로 바꾼다 (호출 횟수도 센다).
Place·정류장은 테스트 트랜잭션 안에 넣는다.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from geoalchemy2.shape import from_shape
from httpx import AsyncClient
from shapely.geometry import LineString, Point
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.tmap import LatLng, PedestrianRoute, get_tmap_client
from app.core.config import get_settings
from app.domain.commute.models import Preference, Station
from app.domain.route.models import LegType, Place, PlaceType, Route
from app.domain.route.service import (
    MAX_WAYPOINTS,
    RankedRoutes,
    RouteService,
    distance_m,
    shortlist,
)
from app.main import app
from tests.conftest import TokenFactory

KST = timezone(timedelta(hours=9))
NOON = datetime(2026, 11, 10, 12, 0, tzinfo=KST)
A = LatLng(lat=35.8600, lng=128.6000)
B = LatLng(lat=35.8600, lng=128.6110)  # A 에서 동쪽 약 1km
WALK_SPEED_M_PER_MIN = 80


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@dataclass
class FakeTmap:
    """출발 → 경유지 → 도착 직선을 돌려준다. 분은 거리 / 80m."""

    calls: list[list[LatLng]] = field(default_factory=list)

    async def pedestrian_route(
        self, start: LatLng, end: LatLng, pass_points: list[LatLng] | None = None
    ) -> PedestrianRoute:
        path = [start, *(pass_points or []), end]
        self.calls.append(path)
        length = sum(distance_m(a, b) for a, b in zip(path, path[1:], strict=False))
        return PedestrianRoute(
            minutes=length / WALK_SPEED_M_PER_MIN, distance_m=int(length), polyline=path
        )


def place(
    name: str, lat: float, lng: float, types: list[PlaceType], pair_id: str | None = None
) -> Place:
    return Place(
        id=uuid4(),
        name=name,
        location=from_shape(Point(lng, lat), srid=4326),
        types=[t.value for t in types],
        description=f"{name} 설명",
        pair_id=pair_id,
        city=get_settings().default_city,
    )


def route(
    *,
    leg_type: LegType = LegType.WALK_ALL,
    is_baseline: bool = False,
    add_minutes: float = 0,
    green_pct: float = 0,
    water_score: float = 0,
    landmark_score: float = 0,
    sunset_ratio: float = 0,
    tags: list[PlaceType] | None = None,
) -> Route:
    return Route(
        id=uuid4(),
        commute_id=uuid4(),
        leg_type=leg_type,
        is_baseline=is_baseline,
        name="테스트",
        minutes=12 + add_minutes,
        add_minutes=add_minutes,
        polyline=from_shape(LineString([(A.lng, A.lat), (B.lng, B.lat)]), srid=4326),
        green_pct=green_pct,
        water_score=water_score,
        landmark_score=landmark_score,
        sunset_ratio=sunset_ratio,
        tags=[t.value for t in (tags or [])],
        spots=[],
        highlights=[],
    )


# --- 규칙 ②③④ (순수) ------------------------------------------------------


def test_river_pair_counts_as_one_waypoint() -> None:
    places = [
        place("신천 진입", 35.8610, 128.6030, [PlaceType.RIVER], pair_id="r"),
        place("신천 진출", 35.8610, 128.6070, [PlaceType.RIVER], pair_id="r"),
        place("공원", 35.8603, 128.6040, [PlaceType.PARK]),
    ]

    waypoints = shortlist(A, B, places, Preference.FULL)

    assert len(waypoints) == 2
    pair = next(w for w in waypoints if len(w.places) == 2)
    assert [p.name for p in pair.places] == ["신천 진입", "신천 진출"]  # 출발점에 가까운 쪽이 먼저
    assert len(pair.pass_points) == 2


def test_sunset_preference_keeps_only_sunset_view_bridge_places() -> None:
    places = [
        place("공원", 35.8603, 128.6040, [PlaceType.PARK]),
        place("다리", 35.8605, 128.6080, [PlaceType.BRIDGE]),
        place("노을터", 35.8597, 128.6060, [PlaceType.SUNSET, PlaceType.VIEW]),
    ]

    names = [w.places[0].name for w in shortlist(A, B, places, Preference.SUNSET)]

    assert names == ["다리", "노을터"] or names == ["노을터", "다리"]


def test_short_preference_drops_large_detour() -> None:
    # 출발점 바로 북쪽 550m: 우회량 ≈ 690m → short(600m) 제외, full(1000m) 포함
    far = place("북쪽공원", 35.8650, 128.6000, [PlaceType.PARK])

    assert shortlist(A, B, [far], Preference.SHORT) == []
    assert len(shortlist(A, B, [far], Preference.FULL)) == 1


def test_shortlist_caps_at_six_by_detour() -> None:
    places = [
        place(f"p{i}", 35.8600 + 0.0001 * (i + 1), 128.6010 + 0.001 * i, [PlaceType.PARK])
        for i in range(9)
    ]

    waypoints = shortlist(A, B, places, Preference.FULL)

    assert len(waypoints) == MAX_WAYPOINTS
    detours = [w.detour_m for w in waypoints]
    assert detours == sorted(detours)


# --- 규칙 ① (DB) ------------------------------------------------------------


async def test_places_beyond_600m_are_not_shortlisted(db_session: AsyncSession) -> None:
    near = place("가까운공원", 35.8603, 128.6040, [PlaceType.PARK])
    far = place("먼공원", 35.8700, 128.6050, [PlaceType.PARK])  # 북쪽 1.1km
    db_session.add_all([near, far])
    await db_session.flush()
    service = RouteService(db_session, FakeTmap())  # type: ignore[arg-type]

    found = await service._places_near([A, B], get_settings().default_city)

    assert [p.name for p in found] == ["가까운공원"]


# --- rank (순수) ---------------------------------------------------------------


def rank(routes: list[Route], preference: Preference = Preference.FULL) -> RankedRoutes:
    service = RouteService.__new__(RouteService)
    return service.rank(routes, preference=preference, at=NOON, is_clear=None)


def test_short_excludes_route_adding_9_minutes() -> None:
    routes = [route(is_baseline=True, green_pct=10), route(add_minutes=9, green_pct=60)]

    assert rank(routes, Preference.SHORT).candidates == []
    assert len(rank(routes, Preference.FULL).candidates) == 1


def test_low_green_route_with_event_10_is_still_a_candidate() -> None:
    bridge = route(add_minutes=3, green_pct=12, landmark_score=10, tags=[PlaceType.BRIDGE])
    routes = [route(is_baseline=True, green_pct=10), bridge]

    ranked = rank(routes)

    assert [r.route.id for r in ranked.candidates] == [bridge.id]
    assert ranked.candidates[0].event_score == 10


def test_rank_orders_by_score_and_keeps_three() -> None:
    baseline = route(is_baseline=True, green_pct=10)
    better = route(add_minutes=5, green_pct=70)
    good = route(add_minutes=5, green_pct=50)
    ok = route(add_minutes=5, green_pct=40)
    weak = route(add_minutes=5, green_pct=30)

    ranked = rank([baseline, weak, ok, good, better])

    assert [r.route.id for r in ranked.candidates] == [better.id, good.id, ok.id]
    assert [r.route.id for r in ranked.eligible] == [better.id, good.id, ok.id, weak.id]
    assert ranked.candidates[0].score == 70 + 0 - 5 * 1.5
    assert [r.route.id for r in ranked.shortest] == [baseline.id]


def test_no_candidates_returns_empty_with_shortest() -> None:
    routes = [route(is_baseline=True, green_pct=10), route(add_minutes=20, green_pct=90)]

    ranked = rank(routes)

    assert ranked.candidates == []
    assert len(ranked.shortest) == 1


# --- API ---------------------------------------------------------------------


def walk_commute() -> dict[str, object]:
    return {
        "from_lat": A.lat,
        "from_lng": A.lng,
        "to_lat": B.lat,
        "to_lng": B.lng,
        "from_area": "테스트동",
        "to_area": "테스트동",
        "go_time": "08:30",
        "leave_time": "18:10",
        "transport_mode": "walk",
    }


async def add_stations(session: AsyncSession, coords: list[tuple[float, float]]) -> list[Station]:
    stations = [
        Station(
            line="2",
            name=f"역{i}",
            location=from_shape(Point(lng, lat), srid=4326),
            order_no=i,
            city=get_settings().default_city,
        )
        for i, (lat, lng) in enumerate(coords)
    ]
    session.add_all(stations)
    await session.flush()
    return stations


@dataclass
class SpurTmap(FakeTmap):
    """경유지가 있으면 출발 → 경유지 → 같은 길로 출발점 → 도착. 공원 안쪽 경유지를 흉내 낸다."""

    async def pedestrian_route(
        self, start: LatLng, end: LatLng, pass_points: list[LatLng] | None = None
    ) -> PedestrianRoute:
        if not pass_points:
            return await super().pedestrian_route(start, end)
        path = [start, *pass_points, start, end]
        self.calls.append(path)
        length = sum(distance_m(a, b) for a, b in zip(path, path[1:], strict=False))
        return PedestrianRoute(
            minutes=length / WALK_SPEED_M_PER_MIN, distance_m=int(length), polyline=path
        )


def use_fake_tmap() -> FakeTmap:
    fake = FakeTmap()
    app.dependency_overrides[get_tmap_client] = lambda: fake
    return fake


async def test_generate_without_token_returns_401(client: AsyncClient) -> None:
    assert (await client.post("/commutes/me/routes")).status_code == 401


async def test_generate_without_commute_returns_404(
    client: AsyncClient, make_token: TokenFactory
) -> None:
    response = await client.post("/commutes/me/routes", headers=bearer(make_token()))

    assert response.status_code == 404


async def test_generate_walk_stores_baseline_and_bridge_candidate(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    db_session.add_all(
        [
            place("공원A", 35.8603, 128.6040, [PlaceType.PARK]),
            place("다리B", 35.8612, 128.6080, [PlaceType.BRIDGE, PlaceType.VIEW]),
        ]
    )
    await db_session.flush()
    fake = use_fake_tmap()
    headers = bearer(make_token())
    await client.put("/commutes/me", json=walk_commute(), headers=headers)

    response = await client.post("/commutes/me/routes", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["preference"] == "full"
    # 기준선 1 + 경유지 2 + 도착 후 집 쪽 1 (#56, 회사 쪽은 #96 에서 제외)
    # (도착 후 반경에서 공원A 는 왕복 우회량 1,264m 라 규칙 ② 에서 제외)
    assert len(fake.calls) == 1 + 2 + 1
    assert [r["leg_type"] for r in body["shortest"]] == ["walk_all"]
    assert body["shortest"][0]["is_baseline"] is True
    # 녹지 데이터가 없어 공원A 는 후보 조건 미달, 다리B 는 이벤트 10 으로 후보
    assert [r["name"] for r in body["candidates"]] == ["다리B 샛길"]
    candidate = body["candidates"][0]
    assert candidate["event_score"] == 10
    assert candidate["tags"] == ["bridge", "view"]
    assert candidate["highlights"] == ["다리B 설명"]
    assert len(candidate["polyline"]) == 3
    assert candidate["add_minutes"] > 0


async def test_generate_twice_replaces_routes(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    db_session.add(place("다리B", 35.8612, 128.6080, [PlaceType.BRIDGE]))
    await db_session.flush()
    use_fake_tmap()
    headers = bearer(make_token())
    await client.put("/commutes/me", json=walk_commute(), headers=headers)

    first = await client.post("/commutes/me/routes", headers=headers)
    second = await client.post("/commutes/me/routes", headers=headers)
    listed = await client.get("/commutes/me/routes", headers=headers)

    assert first.status_code == second.status_code == listed.status_code == 200
    assert len(listed.json()["shortest"]) == 1
    assert listed.json()["candidates"][0]["id"] == second.json()["candidates"][0]["id"]


async def test_get_reranks_with_preference_chip_without_calling_tmap(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    # 다리B 는 노을·전망·다리 계열이라 sunset 취향에서도 남고, 공원A 는 ③에서 빠진다
    db_session.add_all(
        [
            place("공원A", 35.8603, 128.6040, [PlaceType.PARK]),
            place("다리B", 35.8612, 128.6080, [PlaceType.BRIDGE]),
        ]
    )
    await db_session.flush()
    fake = use_fake_tmap()
    headers = bearer(make_token())
    await client.put("/commutes/me", json=walk_commute(), headers=headers)
    await client.post("/commutes/me/routes", headers=headers)
    calls_after_generate = len(fake.calls)

    response = await client.get(
        "/commutes/me/routes", params={"preference": "short"}, headers=headers
    )

    assert response.status_code == 200
    assert response.json()["preference"] == "short"
    assert len(fake.calls) == calls_after_generate


async def test_generate_transit_with_seeded_stations_adds_skip_legs(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    # 시드 정류장 3개(역0→역2): 먼저 내리기·늦게 타기 모두 가운데 역1 하나
    stations = await add_stations(
        db_session, [(35.8600, 128.6030), (35.8600, 128.6060), (35.8600, 128.6090)]
    )
    db_session.add(place("다리B", 35.8612, 128.6080, [PlaceType.BRIDGE]))
    await db_session.flush()
    fake = use_fake_tmap()
    headers = bearer(make_token())
    request = {
        **walk_commute(),
        "transport_mode": "transit",
        "line": "2",
        "station_ids": [str(stations[0].id), str(stations[2].id)],
    }
    await client.put("/commutes/me", json=request, headers=headers)

    response = await client.post("/commutes/me/routes", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert {r["leg_type"] for r in body["shortest"]} == {"front_walk", "back_walk"}
    # 앞·뒤 도보 + 먼저 내리기 1 + 늦게 타기 1 + 도착지 반경
    assert len(fake.calls) <= 5 * (1 + MAX_WAYPOINTS)
    skipped = list(
        (
            await db_session.execute(
                select(Route).where(Route.leg_type.in_(["alight_early", "board_late"]))
            )
        ).scalars()
    )
    assert {r.skip_stops for r in skipped} == {1}
    assert {r.alight_station_id or r.board_station_id for r in skipped} == {stations[1].id}
    assert body["fallback_level"] == 1


async def test_round_trip_into_park_is_not_stored(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    # 출발점 북쪽 150m 다리C 는 이벤트 10, 왕복해도 +3.75분이라 원래 후보다.
    # TMAP 이 들어갔다 같은 길로 나오는 경로를 주면 저장하지 않는다 (#49)
    db_session.add(place("다리C", A.lat + 150 / 110_540, A.lng, [PlaceType.BRIDGE]))
    await db_session.flush()
    app.dependency_overrides[get_tmap_client] = lambda: SpurTmap()
    headers = bearer(make_token())
    await client.put("/commutes/me", json=walk_commute(), headers=headers)

    response = await client.post("/commutes/me/routes", headers=headers)

    assert response.status_code == 200
    # 걷는 구간 후보가 없다 (도착 후 구간은 경유지 없이 한 방향이라 왕복이 아니다)
    assert all(r["leg_type"] != "walk_all" for r in response.json()["candidates"])
