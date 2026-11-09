"""탑승 계획 여러 개일 때의 경로 생성·순위·journey·카드 기대 동작 (#88).

집 → 동대구역(공유) → [402: 달서교회건너 | 156: 대구제일고건너] → 회사.
계획마다 뒤 도보·먼저 내리기 경로를 만들고, 같은 승차 정류장까지의 앞 도보는 TMAP 을 한 번만 부른다.
"""

from uuid import uuid4

from geoalchemy2.shape import from_shape
from httpx import AsyncClient
from shapely.geometry import LineString
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.tmap import LatLng, get_tmap_client
from app.domain.commute.models import Preference
from app.domain.route.models import LegType, PlaceType, Route
from app.domain.route.service import RankedRoutes, RouteService
from app.main import app
from tests.conftest import TokenFactory
from tests.domain.card.test_cards import GOOD_CHOICE, FakeLlm, use_fakes
from tests.domain.commute.test_rides import BUS_156, BUS_402, body_with_options
from tests.domain.route.test_routes import FakeTmap, bearer, place

HOME = LatLng(lat=35.8575, lng=128.6370)  # tests.domain.commute.test_transit.FROM
WORK = LatLng(lat=35.8690, lng=128.5960)  # TO
BOARD = LatLng(lat=35.8600, lng=128.6030)  # 동대구역 (두 노선 공유)
PARK_NEAR_156 = (35.8615, 128.6090)


def use_fake_tmap() -> FakeTmap:
    fake = FakeTmap()
    app.dependency_overrides[get_tmap_client] = lambda: fake
    return fake


async def save_two_rides(client: AsyncClient, headers: dict[str, str]) -> dict[str, object]:
    response = await client.put(
        "/commutes/me", json=body_with_options([BUS_402], [BUS_156]), headers=headers
    )
    assert response.status_code == 200
    return dict(response.json())


async def test_generate_makes_walks_per_ride_and_calls_tmap_once_for_shared_boarding(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    fake = use_fake_tmap()
    headers = bearer(make_token())
    saved = await save_two_rides(client, headers)
    ride_ids = {r["id"] for r in saved["rides"]}  # type: ignore[index]

    response = await client.post("/commutes/me/routes?direction=to_work", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert {r["id"] for r in body["rides"]} == ride_ids
    routes = list((await db_session.execute(select(Route))).scalars())
    front = [r for r in routes if r.leg_type == LegType.FRONT_WALK and r.is_baseline]
    back = [r for r in routes if r.leg_type == LegType.BACK_WALK and r.is_baseline]
    assert {str(r.ride_id) for r in front} == ride_ids  # 계획마다 앞 도보 기준선
    assert {str(r.ride_id) for r in back} == ride_ids  # 계획마다 뒤 도보 기준선
    assert len({r.points[0] for r in back}) == 2  # 하차 정류장이 달라 시작점이 다르다
    # 집 → 동대구역 은 두 계획이 같아서 TMAP 한 번
    assert sum(1 for call in fake.calls if call[0] == HOME and call[-1] == BOARD) == 1
    # 응답의 모든 경로는 어느 계획 것인지 안다 (도보 통근·도착 후만 None)
    for route in [*body["candidates"], *body["shortest"]]:
        if route["leg_type"] != "after_arrival":
            assert route["ride_id"] in ride_ids


async def test_seeded_station_ids_still_make_one_ride(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    from tests.domain.route.test_routes import add_stations, walk_commute

    stations = await add_stations(
        db_session, [(35.8600, 128.6030), (35.8600, 128.6060), (35.8600, 128.6090)]
    )
    await db_session.flush()
    use_fake_tmap()
    headers = bearer(make_token())
    request = {
        **walk_commute(),
        "transport_mode": "transit",
        "line": "2",
        "station_ids": [str(stations[0].id), str(stations[2].id)],
    }
    saved = await client.put("/commutes/me", json=request, headers=headers)
    assert [r["line"] for r in saved.json()["rides"]] == ["2"]

    response = await client.post("/commutes/me/routes", headers=headers)

    assert response.status_code == 200
    assert len(response.json()["rides"]) == 1


# --- 순위: 기준선은 같은 계획의 것 ---------------------------------------------------


def route(
    leg_type: LegType, *, ride_id: object, is_baseline: bool = False, green_pct: float = 0
) -> Route:
    return Route(
        id=uuid4(),
        commute_id=uuid4(),
        ride_id=ride_id,
        leg_type=leg_type,
        is_baseline=is_baseline,
        name="테스트",
        minutes=10,
        add_minutes=3,
        polyline=from_shape(LineString([(128.6, 35.86), (128.61, 35.86)]), srid=4326),
        green_pct=green_pct,
        water_score=0,
        landmark_score=0,
        sunset_ratio=0,
        tags=[],
        spots=[],
        highlights=[],
    )


def rank(routes: list[Route]) -> RankedRoutes:
    from datetime import datetime, timedelta, timezone

    service = RouteService.__new__(RouteService)
    noon = datetime(2026, 11, 10, 12, 0, tzinfo=timezone(timedelta(hours=9)))
    return service.rank(routes, preference=Preference.FULL, at=noon, is_clear=None)


def test_green_gain_is_measured_against_the_candidates_own_ride_baseline() -> None:
    ride_a, ride_b = uuid4(), uuid4()
    routes = [
        route(LegType.BACK_WALK, ride_id=ride_a, is_baseline=True, green_pct=40),
        route(LegType.BACK_WALK, ride_id=ride_b, is_baseline=True, green_pct=0),
        # 156 하차 뒤 공원 길: 자기 기준선(0%) 대비 +12 → 후보. 402 기준선(40%) 과 비교하면 탈락
        route(LegType.BACK_WALK, ride_id=ride_b, green_pct=12),
    ]

    ranked = rank(routes)

    assert [r.route.ride_id for r in ranked.candidates] == [ride_b]
    assert len(ranked.shortest) == 2


# --- journey·카드 -----------------------------------------------------------------


async def test_journey_of_a_ride_candidate_uses_that_rides_stations_and_baselines(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    db_session.add(place("공원", *PARK_NEAR_156, [PlaceType.PARK]))
    await db_session.flush()
    use_fake_tmap()
    headers = bearer(make_token())
    saved = await save_two_rides(client, headers)
    ride_156 = next(r for r in saved["rides"] if r["line"] == "156")  # type: ignore[index]

    response = await client.post("/commutes/me/routes?direction=to_work", headers=headers)

    body = response.json()
    assert body["fallback_level"] == 1
    candidate = next(r for r in body["candidates"] if r["ride_id"] == ride_156["id"])
    kinds = [(s["kind"], s["role"]) for s in candidate["journey"]]
    assert kinds == [("walk", "usual"), ("ride", "usual"), ("walk", "saetgil")]
    ride_segment = candidate["journey"][1]
    assert (ride_segment["line"], ride_segment["from_name"], ride_segment["to_name"]) == (
        "156",
        "동대구역",
        "대구제일고건너",
    )
    for before, after in zip(candidate["journey"], candidate["journey"][1:], strict=False):
        assert before["polyline"][-1] == after["polyline"][0]


async def test_card_today_mixes_candidates_from_every_ride(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    db_session.add(place("공원", *PARK_NEAR_156, [PlaceType.PARK]))
    await db_session.flush()
    use_fake_tmap()
    use_fakes(FakeLlm(choice=GOOD_CHOICE.model_copy(update={"choice": 0})))
    headers = bearer(make_token())
    saved = await save_two_rides(client, headers)
    await client.post("/commutes/me/routes", headers=headers)

    response = await client.get("/cards/today", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert {r["id"] for r in body["rides"]} == {r["id"] for r in saved["rides"]}  # type: ignore[index]
    assert body["route"]["ride_id"] in {r["id"] for r in saved["rides"]}  # type: ignore[index]
