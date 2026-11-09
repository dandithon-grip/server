"""전체 여정(journey) 기대 동작.

서버는 걷는 구간 하나씩을 Route 로 저장한다. 프론트가 "출발 → 정류장 → 탑승 → 정류장 → 도착" 을
한 줄로 그릴 수 있게, 후보마다 그 후보를 골랐을 때의 전체 여정을 조각(segment) 순서로 이어 준다.
샛길 조각은 role=saetgil, 평소 길 조각은 usual, 도착 후 산책은 after_arrival 이다.
"""

from uuid import UUID, uuid4

from geoalchemy2.shape import from_shape
from httpx import AsyncClient
from shapely.geometry import LineString, Point
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.tmap import LatLng
from app.core.config import get_settings
from app.domain.commute.models import Station, TransportMode
from app.domain.route.journey import (
    Segment,
    SegmentKind,
    SegmentRole,
    compose_journey,
    orient_rides,
)
from app.domain.route.models import Direction, LegType, PlaceType, Route
from app.domain.route.service import RankedRoute, oriented
from tests.conftest import TokenFactory
from tests.domain.card.test_cards import GOOD_CHOICE, FakeLlm, use_fakes
from tests.domain.route.test_routes import (
    add_stations,
    bearer,
    place,
    use_fake_tmap,
    walk_commute,
)

HOME = (35.8600, 128.6000)
S0 = (35.8600, 128.6030)
S1 = (35.8600, 128.6060)
S2 = (35.8600, 128.6090)
WORK = (35.8600, 128.6110)
PARK = (35.8612, 128.6100)


def station(name: str, point: tuple[float, float], order_no: int) -> Station:
    lat, lng = point
    return Station(
        id=uuid4(),
        line="2",
        name=name,
        location=from_shape(Point(lng, lat), srid=4326),
        order_no=order_no,
        city=get_settings().default_city,
    )


def route(
    leg_type: LegType,
    points: list[tuple[float, float]],
    *,
    is_baseline: bool = False,
    alight_station_id: UUID | None = None,
    board_station_id: UUID | None = None,
    direction: Direction = Direction.TO_WORK,
) -> Route:
    return Route(
        id=uuid4(),
        commute_id=uuid4(),
        leg_type=leg_type,
        is_baseline=is_baseline,
        name="테스트",
        minutes=10,
        add_minutes=0,
        polyline=from_shape(LineString([(lng, lat) for lat, lng in points]), srid=4326),
        green_pct=0,
        water_score=0,
        landmark_score=0,
        sunset_ratio=0,
        tags=[],
        spots=[],
        highlights=[],
        alight_station_id=alight_station_id,
        board_station_id=board_station_id,
        skip_stops=1 if leg_type in (LegType.ALIGHT_EARLY, LegType.BOARD_LATE) else 0,
        direction=direction,
    )


def ranked(r: Route, direction: Direction = Direction.TO_WORK) -> RankedRoute:
    return oriented(r, direction, score=0.0, event_score=0.0, tags=[])


def kinds(segments: list[Segment]) -> list[tuple[SegmentKind, SegmentRole]]:
    return [(s.kind, s.role) for s in segments]


def assert_connected(segments: list[Segment]) -> None:
    """조각이 끊기지 않고 이어진다: 앞 조각의 끝 = 뒤 조각의 시작."""
    for before, after in zip(segments, segments[1:], strict=False):
        assert before.points[-1] == after.points[0]


STATIONS = [station("역0", S0, 0), station("역1", S1, 1), station("역2", S2, 2)]
BY_ID = {s.id: s for s in STATIONS}
RIDE_TO_WORK = [STATIONS]  # 역0 → 역1 → 역2 한 번 타기
FRONT = route(LegType.FRONT_WALK, [HOME, S0], is_baseline=True)
BACK = route(LegType.BACK_WALK, [S2, WORK], is_baseline=True)
TRIP = (LatLng(lat=HOME[0], lng=HOME[1]), LatLng(lat=WORK[0], lng=WORK[1]))


def compose(
    r: Route,
    *,
    direction: Direction = Direction.TO_WORK,
    baselines: list[Route] = (FRONT, BACK),  # type: ignore[assignment]
    transport_mode: TransportMode = TransportMode.TRANSIT,
) -> list[Segment]:
    shortest = {rk.leg_type: rk for rk in (ranked(b, direction) for b in baselines)}
    trip = TRIP if direction == Direction.TO_WORK else (TRIP[1], TRIP[0])
    transit = transport_mode == TransportMode.TRANSIT
    return compose_journey(
        ranked(r, direction),
        shortest=shortest,
        rides=orient_rides(RIDE_TO_WORK, direction) if transit else [],
        stations=BY_ID,
        transport_mode=transport_mode,
        trip=trip,
    )


# --- 도보 통근 ---------------------------------------------------------------


def test_walk_candidate_is_one_saetgil_segment() -> None:
    base = route(LegType.WALK_ALL, [HOME, WORK], is_baseline=True)
    cand = route(LegType.WALK_ALL, [HOME, PARK, WORK])

    journey = compose(cand, baselines=[base], transport_mode=TransportMode.WALK)

    assert kinds(journey) == [(SegmentKind.WALK, SegmentRole.SAETGIL)]
    assert journey[0].points == [HOME, PARK, WORK]


def test_walk_baseline_is_one_usual_segment() -> None:
    base = route(LegType.WALK_ALL, [HOME, WORK], is_baseline=True)

    journey = compose(base, baselines=[base], transport_mode=TransportMode.WALK)

    assert kinds(journey) == [(SegmentKind.WALK, SegmentRole.USUAL)]


def test_after_arrival_on_walk_appends_to_usual_journey() -> None:
    # 도착 후 산책은 평소 길을 다 간 뒤에 붙는다. 그래서 지도에서 동떨어진 선이 아니다
    base = route(LegType.WALK_ALL, [HOME, WORK], is_baseline=True)
    after = route(LegType.AFTER_ARRIVAL, [WORK, PARK])

    journey = compose(after, baselines=[base], transport_mode=TransportMode.WALK)

    assert kinds(journey) == [
        (SegmentKind.WALK, SegmentRole.USUAL),
        (SegmentKind.WALK, SegmentRole.AFTER_ARRIVAL),
    ]
    assert_connected(journey)


# --- 대중교통 ----------------------------------------------------------------


def test_transit_baseline_is_front_ride_back() -> None:
    journey = compose(FRONT)

    assert kinds(journey) == [
        (SegmentKind.WALK, SegmentRole.USUAL),
        (SegmentKind.RIDE, SegmentRole.USUAL),
        (SegmentKind.WALK, SegmentRole.USUAL),
    ]
    ride = journey[1]
    assert ride.points == [S0, S1, S2]
    assert (ride.line, ride.from_name, ride.to_name) == ("2", "역0", "역2")
    assert_connected(journey)


def test_back_walk_candidate_replaces_only_the_back_segment() -> None:
    cand = route(LegType.BACK_WALK, [S2, PARK, WORK])

    journey = compose(cand)

    assert kinds(journey) == [
        (SegmentKind.WALK, SegmentRole.USUAL),
        (SegmentKind.RIDE, SegmentRole.USUAL),
        (SegmentKind.WALK, SegmentRole.SAETGIL),
    ]
    assert journey[0].points == [HOME, S0]
    assert journey[2].points == [S2, PARK, WORK]
    assert_connected(journey)


def test_board_late_shortens_the_first_ride() -> None:
    # 역1 까지 걸어가서 타면 탑승은 역1 → 역2 뿐이다
    cand = route(LegType.BOARD_LATE, [HOME, PARK, S1], board_station_id=STATIONS[1].id)

    journey = compose(cand)

    assert kinds(journey) == [
        (SegmentKind.WALK, SegmentRole.SAETGIL),
        (SegmentKind.RIDE, SegmentRole.USUAL),
        (SegmentKind.WALK, SegmentRole.USUAL),
    ]
    assert journey[1].points == [S1, S2]
    assert journey[1].from_name == "역1"
    assert_connected(journey)


def test_alight_early_shortens_the_last_ride() -> None:
    cand = route(LegType.ALIGHT_EARLY, [S1, PARK, WORK], alight_station_id=STATIONS[1].id)

    journey = compose(cand)

    assert kinds(journey) == [
        (SegmentKind.WALK, SegmentRole.USUAL),
        (SegmentKind.RIDE, SegmentRole.USUAL),
        (SegmentKind.WALK, SegmentRole.SAETGIL),
    ]
    assert journey[1].points == [S0, S1]
    assert journey[1].to_name == "역1"
    assert_connected(journey)


def test_to_home_reverses_rides_and_starts_at_work() -> None:
    # 퇴근: 회사 → 역2 → 역1 → 역0 → 집. 저장된(출근) 뒤 도보가 퇴근의 앞 도보가 된다
    journey = compose(BACK, direction=Direction.TO_HOME)

    assert journey[0].points == [WORK, S2]
    assert journey[1].points == [S2, S1, S0]
    assert (journey[1].from_name, journey[1].to_name) == ("역2", "역0")
    assert journey[2].points == [S0, HOME]
    assert_connected(journey)


def test_to_home_alight_early_trims_the_ride_near_home() -> None:
    # 출근 때 집 쪽 "걸어가서 타기(역1)" 는 퇴근 때 집 쪽 "먼저 내리기(역1)" 다
    cand = route(LegType.BOARD_LATE, [HOME, PARK, S1], board_station_id=STATIONS[1].id)

    journey = compose(cand, direction=Direction.TO_HOME)

    assert kinds(journey) == [
        (SegmentKind.WALK, SegmentRole.USUAL),
        (SegmentKind.RIDE, SegmentRole.USUAL),
        (SegmentKind.WALK, SegmentRole.SAETGIL),
    ]
    assert journey[1].points == [S2, S1]
    assert journey[2].points == [S1, PARK, HOME]
    assert_connected(journey)


def test_unknown_skip_station_falls_back_to_full_ride() -> None:
    # 정류장 행이 지워졌으면(SET NULL) 탑승을 줄이지 않고 전체로 둔다. 응답이 깨지지 않는다
    cand = route(LegType.BOARD_LATE, [HOME, PARK, S1], board_station_id=uuid4())

    journey = compose(cand)

    assert journey[1].points == [S0, S1, S2]


# --- 자동차 ------------------------------------------------------------------


def test_car_after_arrival_draws_drive_then_walk() -> None:
    after = route(LegType.AFTER_ARRIVAL, [WORK, PARK])

    journey = compose(after, baselines=[], transport_mode=TransportMode.CAR)

    assert kinds(journey) == [
        (SegmentKind.DRIVE, SegmentRole.USUAL),
        (SegmentKind.WALK, SegmentRole.AFTER_ARRIVAL),
    ]
    assert journey[0].points == [HOME, WORK]
    assert_connected(journey)


# --- API ---------------------------------------------------------------------


async def test_routes_api_returns_connected_journey_for_transit(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    stations = await add_stations(db_session, [S0, S1, S2])
    db_session.add(place("공원", PARK[0], PARK[1], [PlaceType.PARK]))
    await db_session.flush()
    use_fake_tmap()
    headers = bearer(make_token())
    request = {
        **walk_commute(),
        "to_lat": WORK[0],
        "to_lng": WORK[1],
        "transport_mode": "transit",
        "line": "2",
        "station_ids": [str(stations[0].id), str(stations[2].id)],
    }
    await client.put("/commutes/me", json=request, headers=headers)

    response = await client.post("/commutes/me/routes?direction=to_work", headers=headers)

    assert response.status_code == 200
    body = response.json()
    for r in [*body["candidates"], *body["shortest"]]:
        journey = r["journey"]
        assert [s["kind"] for s in journey] == ["walk", "ride", "walk"]
        for before, after in zip(journey, journey[1:], strict=False):
            assert before["polyline"][-1] == after["polyline"][0]
        # 전체 여정은 집에서 시작해 회사에서 끝난다
        assert journey[0]["polyline"][0] == {"lat": HOME[0], "lng": HOME[1]}
        assert journey[-1]["polyline"][-1] == {"lat": WORK[0], "lng": WORK[1]}
    saetgil = [s for r in body["candidates"] for s in r["journey"] if s["role"] == "saetgil"]
    assert len(saetgil) == len(body["candidates"])
    assert all(s["role"] == "usual" for r in body["shortest"] for s in r["journey"])


async def test_routes_api_walk_journey_matches_polyline(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    db_session.add(place("공원", 35.8605, 128.6050, [PlaceType.PARK]))
    await db_session.flush()
    use_fake_tmap()
    headers = bearer(make_token())
    await client.put("/commutes/me", json=walk_commute(), headers=headers)

    response = await client.post("/commutes/me/routes", headers=headers)

    assert response.status_code == 200
    for r in [*response.json()["candidates"], *response.json()["shortest"]]:
        assert len(r["journey"]) == 1
        assert r["journey"][0]["kind"] == "walk"
        assert r["journey"][0]["polyline"] == r["polyline"]


async def test_card_routes_also_carry_journey(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    db_session.add(place("공원", 35.8605, 128.6050, [PlaceType.PARK]))
    await db_session.flush()
    use_fake_tmap()
    use_fakes(FakeLlm(choice=GOOD_CHOICE))
    headers = bearer(make_token())
    await client.put("/commutes/me", json=walk_commute(), headers=headers)
    await client.post("/commutes/me/routes", headers=headers)

    response = await client.get("/cards/today", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert all(len(r["journey"]) >= 1 for r in [*body["candidates"], *body["shortest"]])
    if body["route"]:
        assert len(body["route"]["journey"]) >= 1
