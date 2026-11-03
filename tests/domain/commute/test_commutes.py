"""F-02 출퇴근 설정 기대 동작.

- PUT /commutes/me 는 사용자당 1개를 저장한다 (두 번 저장해도 1행)
- transit 인데 정류장이 없거나 모르는 정류장이면 400
- 취향 기본값은 full, 좌표는 저장되고 응답으로 돌아온다
- GET /stations 는 노선 정류장을 순서대로 돌려주고 토큰이 없어도 된다
"""

from uuid import UUID, uuid4

from geoalchemy2.shape import from_shape
from httpx import AsyncClient
from shapely.geometry import Point
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import ErrorCode
from app.domain.commute.models import Commute, Station
from tests.conftest import TokenFactory

LINE = "2"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def add_stations(session: AsyncSession, city: str, names: list[str]) -> list[Station]:
    """테스트용 정류장. 좌표는 대구 근처 임의 값이고 순서만 의미가 있다."""
    stations = [
        Station(
            line=LINE,
            name=name,
            location=from_shape(Point(128.60 + index * 0.01, 35.86), srid=4326),
            order_no=index,
            city=city,
        )
        for index, name in enumerate(names)
    ]
    session.add_all(stations)
    await session.flush()
    return stations


def walk_request() -> dict[str, object]:
    return {
        "from_lat": 35.8581,
        "from_lng": 128.6305,
        "to_lat": 35.8651,
        "to_lng": 128.5935,
        "from_area": "수성구 범어동",
        "to_area": "중구 동성로",
        "go_time": "08:30",
        "leave_time": "18:10",
        "transport_mode": "walk",
    }


def transit_request(station_ids: list[UUID]) -> dict[str, object]:
    return {
        **walk_request(),
        "transport_mode": "transit",
        "line": LINE,
        "station_ids": [str(station_id) for station_id in station_ids],
    }


async def count_commutes(session: AsyncSession) -> int:
    return (await session.execute(select(func.count()).select_from(Commute))).scalar_one()


# --- 저장 -------------------------------------------------------------------


async def test_no_token_returns_401(client: AsyncClient) -> None:
    response = await client.put("/commutes/me", json=walk_request())

    assert response.status_code == 401


async def test_transit_without_stations_returns_400(
    client: AsyncClient, make_token: TokenFactory
) -> None:
    response = await client.put(
        "/commutes/me", json=transit_request([]), headers=bearer(make_token())
    )

    assert response.status_code == 400
    assert response.json() == {"message": ErrorCode.STATION_REQUIRED.message}


async def test_unknown_station_returns_400(client: AsyncClient, make_token: TokenFactory) -> None:
    response = await client.put(
        "/commutes/me", json=transit_request([uuid4()]), headers=bearer(make_token())
    )

    assert response.status_code == 400
    assert response.json() == {"message": ErrorCode.STATION_NOT_FOUND.message}


async def test_preference_defaults_to_full(client: AsyncClient, make_token: TokenFactory) -> None:
    response = await client.put("/commutes/me", json=walk_request(), headers=bearer(make_token()))

    assert response.status_code == 200
    assert response.json()["preference"] == "full"


async def test_saved_coordinates_are_returned(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    response = await client.put("/commutes/me", json=walk_request(), headers=bearer(make_token()))

    assert response.status_code == 200
    body = response.json()
    assert (body["from_lat"], body["from_lng"]) == (35.8581, 128.6305)
    assert (body["to_lat"], body["to_lng"]) == (35.8651, 128.5935)

    # DB 에도 좌표가 들어 있어야 한다 (경로 재계산에 쓴다)
    stmt = select(func.ST_Y(Commute.from_location), func.ST_X(Commute.from_location))
    assert (await db_session.execute(stmt)).one() == (35.8581, 128.6305)


async def test_transit_saves_line_and_stations(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    stations = await add_stations(db_session, get_settings().default_city, ["수성구청", "범어"])
    station_ids = [station.id for station in stations]

    response = await client.put(
        "/commutes/me", json=transit_request(station_ids), headers=bearer(make_token())
    )

    assert response.status_code == 200
    body = response.json()
    assert body["transport_mode"] == "transit"
    assert body["line"] == LINE
    assert [UUID(value) for value in body["station_ids"]] == station_ids


async def test_walk_ignores_stations(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    stations = await add_stations(db_session, get_settings().default_city, ["수성구청"])
    request = {**transit_request([stations[0].id]), "transport_mode": "walk"}

    response = await client.put("/commutes/me", json=request, headers=bearer(make_token()))

    assert response.status_code == 200
    assert response.json()["line"] is None
    assert response.json()["station_ids"] == []


async def test_saving_twice_keeps_one_commute_per_user(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    headers = bearer(make_token())
    first = await client.put("/commutes/me", json=walk_request(), headers=headers)
    updated = {**walk_request(), "leave_time": "19:00", "preference": "sunset"}
    second = await client.put("/commutes/me", json=updated, headers=headers)

    assert first.status_code == second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert second.json()["leave_time"] == "19:00:00"
    assert second.json()["preference"] == "sunset"
    assert await count_commutes(db_session) == 1


# --- 조회 -------------------------------------------------------------------


async def test_get_without_commute_returns_404(
    client: AsyncClient, make_token: TokenFactory
) -> None:
    response = await client.get("/commutes/me", headers=bearer(make_token()))

    assert response.status_code == 404
    assert response.json() == {"message": ErrorCode.COMMUTE_NOT_FOUND.message}


async def test_get_returns_saved_commute(client: AsyncClient, make_token: TokenFactory) -> None:
    headers = bearer(make_token())
    saved = await client.put("/commutes/me", json=walk_request(), headers=headers)

    response = await client.get("/commutes/me", headers=headers)

    assert response.status_code == 200
    assert response.json() == saved.json()


# --- 정류장 -----------------------------------------------------------------


async def test_stations_listed_in_order_without_token(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    city = get_settings().default_city
    await add_stations(db_session, city, ["반월당", "경대병원", "대구은행"])
    await add_stations(db_session, "other-city", ["다른도시역"])

    response = await client.get("/stations", params={"line": LINE})

    assert response.status_code == 200
    body = response.json()
    assert [station["name"] for station in body] == ["반월당", "경대병원", "대구은행"]
    assert [station["order_no"] for station in body] == [0, 1, 2]
    assert body[0]["lat"] == 35.86
