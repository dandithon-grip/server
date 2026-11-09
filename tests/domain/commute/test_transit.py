"""F-02 대중교통 경로 검색·저장 기대 동작. ODsay 와 TMAP 은 가짜로 바꾼다."""

import json
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.odsay import TransitPath, get_odsay_client, parse_search_response
from app.clients.tmap import LatLng, PedestrianRoute, get_tmap_client
from app.core.config import get_settings
from app.core.exceptions import ErrorCode
from app.domain.commute.models import Commute, Station
from app.domain.route.models import LegType, Route
from app.domain.route.service import distance_m
from app.domain.user.models import User
from app.main import app
from tests.conftest import TokenFactory

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "odsay_search_path.json"
FROM = {"from_lat": 35.8575, "from_lng": 128.6370}
TO = {"to_lat": 35.8690, "to_lng": 128.5960}


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@dataclass
class FakeOdsay:
    paths: list[TransitPath]
    calls: int = 0
    error: ErrorCode | None = None

    async def search_transit(self, start: LatLng, end: LatLng) -> list[TransitPath]:
        self.calls += 1
        if self.error is not None:
            from app.core.exceptions import AppException

            raise AppException(self.error)
        return self.paths


@dataclass
class FakeTmap:
    calls: list[list[LatLng]] = field(default_factory=list)

    async def pedestrian_route(
        self, start: LatLng, end: LatLng, pass_points: list[LatLng] | None = None
    ) -> PedestrianRoute:
        path = [start, *(pass_points or []), end]
        self.calls.append(path)
        length = sum(distance_m(a, b) for a, b in zip(path, path[1:], strict=False))
        return PedestrianRoute(minutes=length / 80, distance_m=int(length), polyline=path)


def fixture_paths() -> list[TransitPath]:
    return parse_search_response(json.loads(FIXTURE.read_text(encoding="utf-8")))


def use_fakes(odsay: FakeOdsay | None = None) -> FakeOdsay:
    fake = odsay or FakeOdsay(paths=fixture_paths())
    app.dependency_overrides[get_odsay_client] = lambda: fake
    app.dependency_overrides[get_tmap_client] = lambda: FakeTmap()
    return fake


def legs_of(path: TransitPath) -> list[dict[str, object]]:
    """응답의 탑승 구간을 PUT /commutes/me 의 transit_legs 형식으로."""
    return [
        {
            "line": leg.line,
            "stops": [
                {"name": s.name, "lat": s.lat, "lng": s.lng, "external_id": s.external_id}
                for s in leg.stops
            ],
        }
        for leg in path.legs
        if leg.kind != "walk"
    ]


def commute_body(path: TransitPath) -> dict[str, object]:
    return {
        **FROM,
        **TO,
        "from_area": "수성구 범어동",
        "to_area": "중구 동성로",
        "go_time": "08:30",
        "leave_time": "18:10",
        "transport_mode": "transit",
        "transit_legs": legs_of(path),
    }


def ride(line: str, *stops: tuple[str, float, float, str]) -> dict[str, object]:
    return {
        "line": line,
        "stops": [
            {"name": name, "lat": lat, "lng": lng, "external_id": external_id}
            for name, lat, lng, external_id in stops
        ],
    }


# --- 검색 ---------------------------------------------------------------------


async def test_search_without_token_returns_401(client: AsyncClient) -> None:
    fake = use_fakes()

    response = await client.get("/transit/routes", params={**FROM, **TO})

    assert response.status_code == 401
    assert fake.calls == 0  # ODsay 무료 한도를 비로그인 호출이 소진하지 않는다


async def test_search_returns_paths(client: AsyncClient, make_token: TokenFactory) -> None:
    fake = use_fakes()

    response = await client.get(
        "/transit/routes", params={**FROM, **TO}, headers=bearer(make_token())
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body["paths"]) == 17
    first = body["paths"][0]
    assert first["summary"] == "대구 2호선 수성구청→반월당 · 도보 10분"
    assert first["total_minutes"] == 18
    assert [leg["kind"] for leg in first["legs"]] == ["walk", "subway", "walk"]
    assert len(first["legs"][1]["stops"]) == 5
    # 저장 때 그대로 돌려보낼 수 있게 ODsay 정류장 id 를 내보낸다
    assert all(stop["external_id"] for stop in first["legs"][1]["stops"])
    assert fake.calls == 1


async def test_search_failure_returns_502(client: AsyncClient, make_token: TokenFactory) -> None:
    use_fakes(FakeOdsay(paths=[], error=ErrorCode.EXTERNAL_API_FAILED))

    response = await client.get(
        "/transit/routes", params={**FROM, **TO}, headers=bearer(make_token())
    )

    assert response.status_code == 502


# --- 프론트가 받은 ODsay 응답 해석 (#73) -----------------------------------------
# 운영 서버는 ODsay Server 키의 IP 제한에 걸린다. 프론트가 Web 키로 검색하고 서버는 해석만 한다


async def test_parse_returns_same_shape_as_search(
    client: AsyncClient, make_token: TokenFactory
) -> None:
    fake = use_fakes()
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))

    response = await client.post(
        "/transit/routes/parse", json={"odsay": raw}, headers=bearer(make_token())
    )

    assert response.status_code == 200
    first = response.json()["paths"][0]
    assert len(response.json()["paths"]) == 17
    assert first["summary"] == "대구 2호선 수성구청→반월당 · 도보 10분"
    assert all(stop["external_id"] for stop in first["legs"][1]["stops"])
    assert fake.calls == 0  # 서버는 ODsay 를 부르지 않는다


async def test_parse_odsay_error_object_returns_422(
    client: AsyncClient, make_token: TokenFactory
) -> None:
    use_fakes()
    raw = {"error": {"code": "-98", "msg": "검색 결과가 없습니다"}}

    response = await client.post(
        "/transit/routes/parse", json={"odsay": raw}, headers=bearer(make_token())
    )

    assert response.status_code == 422
    assert response.json() == {"message": ErrorCode.TRANSIT_RESULT_INVALID.message}


async def test_parse_malformed_response_returns_422(
    client: AsyncClient, make_token: TokenFactory
) -> None:
    use_fakes()

    response = await client.post(
        "/transit/routes/parse",
        json={"odsay": {"result": {"path": [{"info": {}}]}}},
        headers=bearer(make_token()),
    )

    assert response.status_code == 422


async def test_parse_without_login_returns_401(client: AsyncClient) -> None:
    response = await client.post("/transit/routes/parse", json={"odsay": {}})

    assert response.status_code == 401


# --- 저장 ---------------------------------------------------------------------


async def test_save_with_transit_legs_upserts_stations(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    use_fakes()
    path = fixture_paths()[0]  # 2호선 수성구청→반월당, 정류장 5개

    response = await client.put(
        "/commutes/me", json=commute_body(path), headers=bearer(make_token())
    )

    assert response.status_code == 200
    body = response.json()
    assert body["line"] == "대구 2호선"
    assert len(body["station_ids"]) == 2  # 탑승·하차
    stations = list((await db_session.execute(select(Station))).scalars())
    assert [s.name for s in sorted(stations, key=lambda s: s.order_no)] == [
        "수성구청",
        "범어",
        "대구은행",
        "경대병원",
        "반월당",
    ]
    assert all(s.line == "대구 2호선" and s.city == get_settings().default_city for s in stations)


async def test_save_twice_does_not_duplicate_stations(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    use_fakes()
    headers = bearer(make_token())
    body = commute_body(fixture_paths()[0])

    await client.put("/commutes/me", json=body, headers=headers)
    await client.put("/commutes/me", json=body, headers=headers)

    assert len(list((await db_session.execute(select(Station))).scalars())) == 5
    assert len(list((await db_session.execute(select(Commute))).scalars())) == 1


async def test_transfer_path_keeps_every_boarding_and_alighting(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    use_fakes()
    path = fixture_paths()[1]  # 2호선 수성구청→반월당, 1호선 반월당→중앙로

    response = await client.put(
        "/commutes/me", json=commute_body(path), headers=bearer(make_token())
    )

    assert response.status_code == 200
    assert response.json()["line"] == "대구 1호선"
    assert len(response.json()["station_ids"]) == 4
    # 반월당은 2호선·1호선 두 행 (노선이 다르면 다른 정류장)
    stations = list((await db_session.execute(select(Station))).scalars())
    assert sum(1 for s in stations if s.name == "반월당") == 2


async def test_transit_without_legs_or_ids_returns_400(
    client: AsyncClient, make_token: TokenFactory
) -> None:
    use_fakes()
    body = {**commute_body(fixture_paths()[0]), "transit_legs": []}

    response = await client.put("/commutes/me", json=body, headers=bearer(make_token()))

    assert response.status_code == 400
    assert response.json() == {"message": ErrorCode.STATION_REQUIRED.message}


# --- 1~3 정거장 먼저 내리기 · 늦게 타기 (#51) -------------------------------------


async def station_names(session: AsyncSession, ids: list[UUID]) -> list[str]:
    rows = (await session.execute(select(Station).where(Station.id.in_(ids)))).scalars()
    by_id = {s.id: s.name for s in rows}
    return [by_id[i] for i in ids]


async def skip_routes(session: AsyncSession, leg_type: LegType) -> dict[int, str]:
    """구간 종류의 경로를 건너뛴 정거장 수 → 정류장 이름으로. 경유지 없는 길만 본다."""
    rows = list(
        (
            await session.execute(
                select(Route).where(Route.leg_type == leg_type, Route.spots == [])
            )
        ).scalars()
    )
    result: dict[int, str] = {}
    for route in rows:
        station_id = route.alight_station_id or route.board_station_id
        assert station_id is not None
        result[route.skip_stops] = (await station_names(session, [station_id]))[0]
    return result


BUS_309 = ride(
    "309",
    ("경신고등학교건너", 35.85904, 128.637335, "431197"),
    ("수성경찰서건너", 35.859007, 128.63336, "431202"),
    ("수성구청건너", 35.859003, 128.630725, "431200"),
    ("범어역(1번출구)", 35.859645, 128.621839, "431213"),
    ("수성네거리1", 35.859817, 128.618187, "431211"),
    ("대구은행역(1번출구)", 35.860061, 128.61273, "431218"),
    ("수성교2", 35.860803, 128.610388, "431217"),
    ("방천시장(김광석길)건너", 35.862774, 128.605698, "431219"),
    ("경북대학교치과병원", 35.86366, 128.601441, "432063"),
    ("삼덕교회", 35.866155, 128.600054, "432061"),
    ("CGV대구한일건너", 35.870851, 128.596335, "432057"),
)


async def test_save_keeps_up_to_three_stops_on_each_side(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    use_fakes()
    body = {**commute_body(fixture_paths()[0]), "transit_legs": [BUS_309]}

    await client.put("/commutes/me", json=body, headers=bearer(make_token()))

    commute = (await db_session.execute(select(Commute))).scalar_one()
    assert await station_names(db_session, commute.alight_early_station_ids) == [
        "삼덕교회",
        "경북대학교치과병원",
        "방천시장(김광석길)건너",
    ]
    assert await station_names(db_session, commute.board_late_station_ids) == [
        "수성경찰서건너",
        "수성구청건너",
        "범어역(1번출구)",
    ]


async def test_short_ride_cannot_skip_its_own_end_stops(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    # 3정거장 구간: 먼저 내리기·늦게 타기 모두 가운데 한 정거장뿐
    use_fakes()
    short = ride(
        "410",
        ("가", 35.8600, 128.6000, "5001"),
        ("나", 35.8610, 128.6010, "5002"),
        ("다", 35.8620, 128.6020, "5003"),
    )
    body = {**commute_body(fixture_paths()[0]), "transit_legs": [short]}

    await client.put("/commutes/me", json=body, headers=bearer(make_token()))

    commute = (await db_session.execute(select(Commute))).scalar_one()
    assert await station_names(db_session, commute.alight_early_station_ids) == ["나"]
    assert await station_names(db_session, commute.board_late_station_ids) == ["나"]


async def test_transfer_uses_first_ride_for_board_late_and_last_for_alight_early(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    # 2호선 수성구청→반월당(5정거장) 환승 1호선 반월당→중앙로(2정거장)
    use_fakes()
    await client.put(
        "/commutes/me", json=commute_body(fixture_paths()[1]), headers=bearer(make_token())
    )

    commute = (await db_session.execute(select(Commute))).scalar_one()
    assert await station_names(db_session, commute.board_late_station_ids) == [
        "범어",
        "대구은행",
        "경대병원",
    ]
    assert commute.alight_early_station_ids == []  # 마지막 구간이 두 정거장뿐


async def test_routes_make_one_leg_per_skipped_stop(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    use_fakes()
    headers = bearer(make_token())
    body = {**commute_body(fixture_paths()[0]), "transit_legs": [BUS_309]}
    await client.put("/commutes/me", json=body, headers=headers)

    response = await client.post("/commutes/me/routes", headers=headers)

    assert response.status_code == 200
    assert await skip_routes(db_session, LegType.ALIGHT_EARLY) == {
        1: "삼덕교회",
        2: "경북대학교치과병원",
        3: "방천시장(김광석길)건너",
    }
    assert await skip_routes(db_session, LegType.BOARD_LATE) == {
        1: "수성경찰서건너",
        2: "수성구청건너",
        3: "범어역(1번출구)",
    }
    # 기준선은 원래 계획의 두 구간뿐이다. 건너뛰고 그냥 걷는 길도 후보 경쟁에 들어간다
    assert {r["leg_type"] for r in response.json()["shortest"]} == {"front_walk", "back_walk"}


# --- 사용자끼리 정류장을 덮어쓰지 않는다 (#44) -------------------------------------


async def test_opposite_direction_stop_with_same_name_is_separate_station(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    # 309번 정방향 A 와 역방향 B. "신천시장" 은 길 건너편 같은 이름 정류장이다
    use_fakes()
    outbound = ride(
        "309",
        ("범어네거리", 35.8590, 128.6250, "1001"),
        ("신천시장", 35.8620, 128.6130, "1002"),
        ("동신교", 35.8640, 128.6050, "1003"),
    )
    inbound = ride(
        "309",
        ("동신교", 35.8642, 128.6052, "2003"),
        ("신천시장", 35.8623, 128.6133, "2002"),
        ("범어네거리", 35.8593, 128.6253, "2001"),
    )
    await client.put(
        "/commutes/me",
        json={**commute_body(fixture_paths()[0]), "transit_legs": [outbound]},
        headers=bearer(make_token(sub="user-a")),
    )

    await client.put(
        "/commutes/me",
        json={**commute_body(fixture_paths()[0]), "transit_legs": [inbound]},
        headers=bearer(make_token(sub="user-b")),
    )

    rows = list(
        (await db_session.execute(select(Station).where(Station.name == "신천시장"))).scalars()
    )
    assert sorted((round(s.lat, 4), s.external_id) for s in rows) == [
        (35.862, "1002"),
        (35.8623, "2002"),
    ]


async def test_other_users_segment_does_not_change_alight_early(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    # A 는 2호선 수성구청→반월당 (하차 직전 경대병원). B 는 같은 노선 다른 구간을 탄다.
    # 구간마다 0 부터 매긴 순번이 겹쳐도 A 의 "한 정거장 먼저" 는 경대병원이어야 한다
    use_fakes()
    headers_a = bearer(make_token(sub="user-a"))
    await client.put("/commutes/me", json=commute_body(fixture_paths()[0]), headers=headers_a)
    other = ride(
        "대구 2호선",
        ("다사", 35.8600, 128.4600, "9001"),
        ("대실", 35.8610, 128.4700, "9002"),
        ("강창", 35.8620, 128.4800, "9003"),
        ("계명대", 35.8530, 128.4900, "9004"),
    )
    await client.put(
        "/commutes/me",
        json={**commute_body(fixture_paths()[0]), "transit_legs": [other]},
        headers=bearer(make_token(sub="user-b")),
    )

    response = await client.post("/commutes/me/routes", headers=headers_a)

    assert response.status_code == 200
    # 같은 트랜잭션에서 만든 두 동선은 created_at 이 같아 순서로 고를 수 없다. 사용자로 찾는다
    commute_a = (
        await db_session.execute(
            select(Commute)
            .join(User, User.id == Commute.user_id)
            .where(User.supabase_id == "user-a")
        )
    ).scalar_one()
    assert (await station_names(db_session, commute_a.alight_early_station_ids))[0] == "경대병원"


async def test_transit_stop_without_external_id_returns_422(
    client: AsyncClient, make_token: TokenFactory
) -> None:
    use_fakes()
    leg = {"line": "309", "stops": [{"name": "가", "lat": 35.86, "lng": 128.6}] * 2}
    body = {**commute_body(fixture_paths()[0]), "transit_legs": [leg]}

    response = await client.put("/commutes/me", json=body, headers=bearer(make_token()))

    assert response.status_code == 422


async def test_loop_bus_passing_same_stop_twice_saves(
    client: AsyncClient, make_token: TokenFactory
) -> None:
    # 순환 버스는 한 구간에 같은 정류장이 두 번 나온다.
    # 한 번의 upsert 에 같은 키가 둘이면 500 이었다
    use_fakes()
    loop = ride(
        "순환2",
        ("가", 35.8600, 128.6000, "3001"),
        ("나", 35.8610, 128.6010, "3002"),
        ("가", 35.8600, 128.6000, "3001"),
    )
    body = {**commute_body(fixture_paths()[0]), "transit_legs": [loop]}

    response = await client.put("/commutes/me", json=body, headers=bearer(make_token()))

    assert response.status_code == 200
