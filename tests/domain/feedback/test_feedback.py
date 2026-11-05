"""F-16 피드백 → 태그 가중치 기대 동작."""

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from geoalchemy2.shape import from_shape
from httpx import AsyncClient
from shapely.geometry import LineString
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ErrorCode
from app.domain.commute.models import Commute, Preference
from app.domain.feedback.service import apply_thumbs, top_tags
from app.domain.route.models import LegType, PlaceType, Route
from app.domain.route.service import RouteService
from app.domain.user.models import User
from tests.conftest import TokenFactory

A = (35.8600, 128.6000)
B = (35.8600, 128.6110)
KST = timezone(timedelta(hours=9))
NOON = datetime(2026, 11, 10, 12, 0, tzinfo=KST)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def walk_commute() -> dict[str, object]:
    return {
        "from_lat": A[0],
        "from_lng": A[1],
        "to_lat": B[0],
        "to_lng": B[1],
        "from_area": "테스트동",
        "to_area": "테스트동",
        "go_time": "08:30",
        "leave_time": "18:10",
        "transport_mode": "walk",
    }


def make_route(
    commute_id: UUID,
    *,
    is_baseline: bool = False,
    add_minutes: float = 5,
    green_pct: float = 40,
    tags: list[PlaceType] | None = None,
    sunset_ratio: float = 0,
) -> Route:
    return Route(
        id=uuid4(),
        commute_id=commute_id,
        leg_type=LegType.WALK_ALL,
        is_baseline=is_baseline,
        name="테스트",
        minutes=12 + add_minutes,
        add_minutes=add_minutes,
        polyline=from_shape(LineString([(A[1], A[0]), (B[1], B[0])]), srid=4326),
        green_pct=green_pct,
        water_score=0,
        landmark_score=0,
        sunset_ratio=sunset_ratio,
        tags=[t.value for t in (tags or [])],
        spots=[],
        highlights=[],
    )


async def prepare(
    client: AsyncClient, session: AsyncSession, headers: dict[str, str]
) -> tuple[User, Route]:
    """동선 + 하천 샛길 1개. (사용자, 경로) 를 돌려준다."""
    await client.put("/commutes/me", json=walk_commute(), headers=headers)
    user = (await session.execute(select(User))).scalar_one()
    commute = (await session.execute(select(Commute))).scalar_one()
    river = make_route(commute.id, tags=[PlaceType.RIVER, PlaceType.PARK])
    session.add_all([make_route(commute.id, is_baseline=True, green_pct=10), river])
    await session.flush()
    return user, river


async def refreshed_weights(session: AsyncSession, user: User) -> dict[str, float]:
    await session.refresh(user)
    return user.tag_weights


# --- 순수 계산 ----------------------------------------------------------------


def test_apply_thumbs_adds_and_clamps() -> None:
    weights = apply_thumbs({}, [PlaceType.RIVER], direction=1)
    assert weights == {"river": 0.2}

    for _ in range(6):
        weights = apply_thumbs(weights, [PlaceType.RIVER], direction=1)
    assert weights["river"] == 1.0  # 상한

    assert apply_thumbs({"park": -1.0}, [PlaceType.PARK], direction=-1) == {"park": -1.0}


def test_top_tags_returns_positive_top_two() -> None:
    assert top_tags({"river": 0.6, "park": -0.2, "trees": 0.2, "sunset": 0.4}) == [
        "river",
        "sunset",
    ]
    assert top_tags({}) == []


def test_weights_change_ranking_order() -> None:
    commute_id = uuid4()
    baseline = make_route(commute_id, is_baseline=True, green_pct=10)
    park = make_route(commute_id, green_pct=60, tags=[PlaceType.PARK])
    river = make_route(commute_id, green_pct=50, tags=[PlaceType.RIVER, PlaceType.TREES])
    service = RouteService.__new__(RouteService)

    plain = service.rank(
        [baseline, park, river], preference=Preference.FULL, at=NOON, is_clear=None
    )
    boosted = service.rank(
        [baseline, park, river],
        preference=Preference.FULL,
        at=NOON,
        is_clear=None,
        tag_weights={"river": 1.0, "trees": 1.0},
    )

    assert [r.route.id for r in plain.candidates] == [park.id, river.id]  # 52.5 > 42.5
    assert [r.route.id for r in boosted.candidates] == [river.id, park.id]  # river +20 → 62.5


# --- API ----------------------------------------------------------------------


async def test_thumbs_up_adds_0_2_to_route_tags(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    headers = bearer(make_token())
    user, river = await prepare(client, db_session, headers)

    response = await client.post(
        "/feedback", json={"route_id": str(river.id), "thumbs": "up"}, headers=headers
    )

    assert response.status_code == 200
    assert response.json()["weights"] == {"river": 0.2, "park": 0.2}
    assert await refreshed_weights(db_session, user) == {"river": 0.2, "park": 0.2}


async def test_thumbs_down_subtracts_and_switching_reverses(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    headers = bearer(make_token())
    user, river = await prepare(client, db_session, headers)
    body = {"route_id": str(river.id), "thumbs": "down"}

    await client.post("/feedback", json=body, headers=headers)
    assert await refreshed_weights(db_session, user) == {"river": -0.2, "park": -0.2}

    # 같은 날 같은 경로에 👍 로 바꾸면 −0.2 를 되돌리고 +0.2
    await client.post("/feedback", json={**body, "thumbs": "up"}, headers=headers)
    assert await refreshed_weights(db_session, user) == {"river": 0.2, "park": 0.2}

    # 같은 걸 또 눌러도 변화 없음
    await client.post("/feedback", json={**body, "thumbs": "up"}, headers=headers)
    assert await refreshed_weights(db_session, user) == {"river": 0.2, "park": 0.2}


async def test_hint_after_three_ups_on_same_tag(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    headers = bearer(make_token())
    user, river = await prepare(client, db_session, headers)
    commute = (await db_session.execute(select(Commute))).scalar_one()
    more = [make_route(commute.id, tags=[PlaceType.RIVER]) for _ in range(2)]
    db_session.add_all(more)
    await db_session.flush()

    for route in [river, *more[:1]]:
        await client.post(
            "/feedback", json={"route_id": str(route.id), "thumbs": "up"}, headers=headers
        )
    before = await client.get("/users/me/tag-weights", headers=headers)
    await client.post(
        "/feedback", json={"route_id": str(more[1].id), "thumbs": "up"}, headers=headers
    )
    after = await client.get("/users/me/tag-weights", headers=headers)

    assert before.json()["hint"] is None  # 2회
    assert after.json()["hint"] == {"tag": "river", "message": "요즘 하천길을 좋아하시네요"}
    assert after.json()["weights"]["river"] == 0.6


async def test_reset_clears_weights_but_keeps_feedback(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    headers = bearer(make_token())
    user, river = await prepare(client, db_session, headers)
    await client.post(
        "/feedback", json={"route_id": str(river.id), "thumbs": "up"}, headers=headers
    )

    response = await client.delete("/users/me/tag-weights", headers=headers)

    assert response.status_code == 200
    assert response.json()["weights"] == {}
    assert await refreshed_weights(db_session, user) == {}


async def test_feedback_on_someone_elses_route_returns_404(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    headers = bearer(make_token())
    await prepare(client, db_session, headers)

    response = await client.post(
        "/feedback", json={"route_id": str(uuid4()), "thumbs": "up"}, headers=headers
    )

    assert response.status_code == 404
    assert response.json() == {"message": ErrorCode.ROUTE_NOT_FOUND.message}


async def test_feedback_without_token_returns_401(client: AsyncClient) -> None:
    response = await client.post("/feedback", json={"route_id": str(uuid4()), "thumbs": "up"})

    assert response.status_code == 401


async def test_routes_endpoint_applies_weights(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    headers = bearer(make_token())
    user, river = await prepare(client, db_session, headers)
    commute = (await db_session.execute(select(Commute))).scalar_one()
    # 하천길(river·park) 녹지 40, 공원길(park) 녹지 48.
    # 👍 5번이면 river·park 가중치 1.0 → 하천길 +20, 공원길 +10 으로 역전
    park = make_route(commute.id, green_pct=48, tags=[PlaceType.PARK])
    # 같은 날 같은 경로는 한 번만 세므로, 하천길 5개에 각각 👍
    others = [make_route(commute.id, tags=[PlaceType.RIVER, PlaceType.PARK]) for _ in range(4)]
    db_session.add_all([park, *others])
    await db_session.flush()
    plain = await client.get("/commutes/me/routes", headers=headers)
    for liked in [river, *others]:
        await client.post(
            "/feedback", json={"route_id": str(liked.id), "thumbs": "up"}, headers=headers
        )

    boosted = await client.get("/commutes/me/routes", headers=headers)

    assert plain.json()["candidates"][0]["id"] == str(park.id)
    assert boosted.json()["candidates"][0]["id"] == str(river.id)
