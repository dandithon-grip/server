"""평소 타는 노선 여러 개(≤3) 저장 기대 동작 (#88).

동선 하나에 탑승 계획(ride)을 최대 3개 둔다. 기존 `transit_legs` 한 세트와 시드 `station_ids` 는
계획 1개로 그대로 동작한다. 기존 `station_ids`·`line` 응답은 첫 번째 계획의 거울이다.
"""

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.commute.models import CommuteRide
from tests.conftest import TokenFactory
from tests.domain.commute.test_transit import (
    FROM,
    TO,
    bearer,
    commute_body,
    fixture_paths,
    ride,
    use_fakes,
)

BUS_402 = ride(
    "402",
    ("동대구역", 35.8600, 128.6030, "d1"),
    ("신천역", 35.8600, 128.6060, "d2"),
    ("달서교회건너", 35.8600, 128.6085, "d3"),
)
BUS_156 = ride(
    "156",
    ("동대구역", 35.8600, 128.6030, "d1"),
    ("동부정류장", 35.8605, 128.6055, "e2"),
    ("대구제일고건너", 35.8608, 128.6095, "e3"),
)
SUBWAY_1 = ride(
    "대구 1호선",
    ("동대구", 35.8601, 128.6028, "s1"),
    ("큰고개", 35.8602, 128.6050, "s2"),
    ("아양교", 35.8603, 128.6080, "s3"),
)


def body_with_options(*options: list[dict[str, object]]) -> dict[str, object]:
    return {
        **FROM,
        **TO,
        "from_area": "동구 신암동",
        "to_area": "서구 비산동",
        "go_time": "08:30",
        "leave_time": "18:10",
        "transport_mode": "transit",
        "transit_options": [list(option) for option in options],
    }


async def test_save_transit_options_stores_each_ride(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    use_fakes()

    response = await client.put(
        "/commutes/me", json=body_with_options([BUS_402], [BUS_156]), headers=bearer(make_token())
    )

    assert response.status_code == 200
    body = response.json()
    assert [
        (r["order_no"], r["line"], r["board_name"], r["alight_name"]) for r in body["rides"]
    ] == [
        (0, "402", "동대구역", "달서교회건너"),
        (1, "156", "동대구역", "대구제일고건너"),
    ]
    # 기존 필드는 첫 번째 계획의 거울
    assert body["line"] == "402"
    assert body["station_ids"] == body["rides"][0]["station_ids"]
    rows = list((await db_session.execute(select(CommuteRide))).scalars())
    assert len(rows) == 2


async def test_save_again_replaces_rides(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    use_fakes()
    headers = bearer(make_token())
    await client.put("/commutes/me", json=body_with_options([BUS_402], [BUS_156]), headers=headers)

    response = await client.put("/commutes/me", json=body_with_options([SUBWAY_1]), headers=headers)

    assert response.status_code == 200
    assert [r["line"] for r in response.json()["rides"]] == ["대구 1호선"]
    rows = list((await db_session.execute(select(CommuteRide))).scalars())
    assert len(rows) == 1


async def test_legacy_transit_legs_is_one_ride(
    client: AsyncClient, make_token: TokenFactory
) -> None:
    use_fakes()

    response = await client.put(
        "/commutes/me", json=commute_body(fixture_paths()[0]), headers=bearer(make_token())
    )

    assert response.status_code == 200
    rides = response.json()["rides"]
    assert len(rides) == 1
    assert (rides[0]["line"], rides[0]["board_name"], rides[0]["alight_name"]) == (
        "대구 2호선",
        "수성구청",
        "반월당",
    )


async def test_more_than_three_options_returns_422(
    client: AsyncClient, make_token: TokenFactory
) -> None:
    use_fakes()
    body = body_with_options([BUS_402], [BUS_156], [SUBWAY_1], [BUS_402])

    response = await client.put("/commutes/me", json=body, headers=bearer(make_token()))

    assert response.status_code == 422


async def test_walk_commute_has_no_rides(client: AsyncClient, make_token: TokenFactory) -> None:
    use_fakes()
    body = {**body_with_options(), "transport_mode": "walk"}

    response = await client.put("/commutes/me", json=body, headers=bearer(make_token()))

    assert response.status_code == 200
    assert response.json()["rides"] == []


async def test_transfer_ride_label_joins_every_line(
    client: AsyncClient, make_token: TokenFactory
) -> None:
    # 환승 경로(2호선 → 1호선)는 마지막 노선만이 아니라 전부 보여야 한다 (#93)
    use_fakes()

    response = await client.put(
        "/commutes/me", json=commute_body(fixture_paths()[1]), headers=bearer(make_token())
    )

    assert response.status_code == 200
    assert response.json()["rides"][0]["line"] == "대구 2호선 → 대구 1호선"
    assert response.json()["line"] == "대구 1호선"  # 거울 열은 그대로 마지막 노선


async def test_ride_keeps_the_exact_stops_of_the_saved_leg(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    use_fakes()

    response = await client.put(
        "/commutes/me", json=body_with_options([BUS_402]), headers=bearer(make_token())
    )

    ride = (await db_session.execute(select(CommuteRide))).scalar_one()
    assert response.status_code == 200
    assert ride.stop_ids is not None and len(ride.stop_ids) == 1
    assert len(ride.stop_ids[0]) == 3  # 동대구역 · 신천역 · 달서교회건너
    assert ride.stop_ids[0][0] == str(ride.station_ids[0])
    assert ride.stop_ids[0][-1] == str(ride.station_ids[-1])
