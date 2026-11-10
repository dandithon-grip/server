"""후보 0건이면 가까운 자연 1곳 '잠깐 들르기' 후보 (#116).

자연이 걷는 구간 옆·너머에 있으면 경유 경로가 왕복이라 전부 걸러져 카드가 빈다.
그때만 양 끝 800m 안 가장 가까운 자연으로 왕복 경로 1개를 만들고, 8분 넘게 더 걸리면 버린다.
"""

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.tmap import LatLng
from app.domain.route.models import PlaceType, Route
from app.domain.route.service import DROP_BY_MAX_ADD_MINUTES, DROP_BY_RADIUS_M
from tests.conftest import TokenFactory
from tests.domain.route.test_routes import WALK_SPEED_M_PER_MIN, bearer, place, use_fake_tmap

# 걷는 구간 100m 짜리 도보 통근. 자연은 도착점 너머 진행 방향 연장선 위라, 들르면 같은 길로 돌아온다
START = LatLng(lat=35.8600, lng=128.6000)
END = LatLng(lat=35.8600, lng=128.6011)  # 약 100m 동쪽
M_PER_DEG_LNG = 90_300  # 위도 35.86 부근


def east_of(point: LatLng, meters: float) -> tuple[float, float]:
    return (point.lat, point.lng + meters / M_PER_DEG_LNG)


def short_walk_commute() -> dict[str, object]:
    return {
        "from_lat": START.lat,
        "from_lng": START.lng,
        "to_lat": END.lat,
        "to_lng": END.lng,
        "from_area": "테스트동",
        "to_area": "테스트동",
        "go_time": "08:30",
        "leave_time": "18:10",
        "transport_mode": "walk",
    }


async def saved_routes(session: AsyncSession) -> list[Route]:
    return list((await session.execute(select(Route))).scalars())


async def test_only_round_trip_nature_becomes_a_drop_by_candidate(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    # 도착점에서 250m 더 간 곳에 공원. 경유 경로는 왕복 250m → 왕복 필터 탈락 → 후보 0건
    park = place("달성공원 서쪽 입구", *east_of(END, 250), [PlaceType.PARK])
    db_session.add(park)
    await db_session.flush()
    fake = use_fake_tmap()
    headers = bearer(make_token())
    await client.put("/commutes/me", json=short_walk_commute(), headers=headers)

    response = await client.post("/commutes/me/routes", headers=headers)

    assert response.status_code == 200
    body = response.json()
    drop_by = [r for r in body["candidates"] if r["drop_by"]]
    assert len(drop_by) == 1
    candidate = drop_by[0]
    assert candidate["leg_type"] == "walk_all"
    assert candidate["name"] == "달성공원 서쪽 입구 잠깐 들르기"
    assert candidate["spots"] == [str(park.id)]
    # 왕복 500m 이 더 걸린다: 500m / 80m/분 ≈ 6.3분 (상한 8분 안)
    assert abs(candidate["add_minutes"] - 500 / WALK_SPEED_M_PER_MIN) < 0.3
    assert body["action_type"] == "drop_by"
    assert body["fallback_level"] == 1
    # TMAP: 기준선 1 + 경유지 1(왕복이라 탈락) + 들르기 1
    assert len([c for c in fake.calls if c[0] == START]) == 3
    stored = [r for r in await saved_routes(db_session) if r.drop_by]
    assert len(stored) == 1 and stored[0].name_reverse == "달성공원 서쪽 입구 잠깐 들르기"


async def test_drop_by_is_skipped_when_a_real_candidate_exists(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    # 구간 가운데에서 북쪽 300m 공원은 V 자로 지나갈 수 있어 보통 후보가 된다 → 들르기 없음
    mid_lat, mid_lng = START.lat + 300 / 110_540, (START.lng + END.lng) / 2
    db_session.add(place("가운데 공원", mid_lat, mid_lng, [PlaceType.PARK]))
    await db_session.flush()
    fake = use_fake_tmap()
    headers = bearer(make_token())
    await client.put("/commutes/me", json=short_walk_commute(), headers=headers)

    response = await client.post("/commutes/me/routes", headers=headers)

    assert response.status_code == 200
    assert not any(r.drop_by for r in await saved_routes(db_session))
    assert len([c for c in fake.calls if c[0] == START]) == 2  # 기준선 + 경유지, 들르기 호출 없음


async def test_drop_by_beyond_the_minute_cap_leaves_the_card_empty(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    # 도착점에서 400m 너머: 왕복 800m ≈ 10분 > 8분 → 들르기도 버린다
    db_session.add(place("먼 공원", *east_of(END, 400), [PlaceType.PARK]))
    await db_session.flush()
    use_fake_tmap()
    headers = bearer(make_token())
    await client.put("/commutes/me", json=short_walk_commute(), headers=headers)

    response = await client.post("/commutes/me/routes", headers=headers)

    assert response.status_code == 200
    assert response.json()["candidates"] == []
    assert not any(r.drop_by for r in await saved_routes(db_session))
    assert 800 / WALK_SPEED_M_PER_MIN > DROP_BY_MAX_ADD_MINUTES


async def test_no_nature_within_radius_keeps_level_4(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    db_session.add(place("아주 먼 공원", *east_of(END, DROP_BY_RADIUS_M + 300), [PlaceType.PARK]))
    await db_session.flush()
    fake = use_fake_tmap()
    headers = bearer(make_token())
    await client.put("/commutes/me", json=short_walk_commute(), headers=headers)

    response = await client.post("/commutes/me/routes", headers=headers)

    assert response.status_code == 200
    assert response.json()["candidates"] == []
    assert len([c for c in fake.calls if c[0] == START]) == 1  # 기준선뿐
