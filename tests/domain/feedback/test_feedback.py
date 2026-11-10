"""F-16 별점 피드백 → 태그 가중치 기대 동작 (#50)."""

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from geoalchemy2.shape import from_shape
from httpx import AsyncClient
from shapely.geometry import LineString, Point
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import ErrorCode
from app.domain.commute.models import Commute, Preference
from app.domain.feedback.models import Feedback
from app.domain.feedback.service import apply_delta, rating_delta, top_tags
from app.domain.route.models import LegType, Place, PlaceType, Route
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
    spots: list[UUID] | None = None,
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
        spots=list(spots or []),
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


def test_rating_delta_is_centered_on_three() -> None:
    assert [rating_delta(r) for r in (1, 2, 3, 4, 5)] == [-0.2, -0.1, 0.0, 0.1, 0.2]


def test_apply_delta_adds_and_clamps() -> None:
    weights = apply_delta({}, [PlaceType.RIVER], 0.2)
    assert weights == {"river": 0.2}

    for _ in range(6):
        weights = apply_delta(weights, [PlaceType.RIVER], 0.2)
    assert weights["river"] == 1.0  # 상한

    assert apply_delta({"park": -1.0}, [PlaceType.PARK], -0.2) == {"park": -1.0}


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


async def rate(
    client: AsyncClient, headers: dict[str, str], route: Route, rating: int
) -> dict[str, object]:
    response = await client.post(
        "/feedback", json={"route_id": str(route.id), "rating": rating}, headers=headers
    )
    assert response.status_code == 200, response.text
    body: dict[str, object] = response.json()
    return body


async def test_five_stars_adds_0_2_and_four_stars_0_1(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    headers = bearer(make_token())
    user, river = await prepare(client, db_session, headers)
    commute = (await db_session.execute(select(Commute))).scalar_one()
    park = make_route(commute.id, tags=[PlaceType.PARK])
    db_session.add(park)
    await db_session.flush()

    body = await rate(client, headers, river, 5)
    await rate(client, headers, park, 4)

    assert body["rating"] == 5
    assert body["weights"] == {"river": 0.2, "park": 0.2}
    assert await refreshed_weights(db_session, user) == {"river": 0.2, "park": 0.3}


async def test_three_stars_changes_nothing(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    headers = bearer(make_token())
    user, river = await prepare(client, db_session, headers)

    await rate(client, headers, river, 3)

    assert await refreshed_weights(db_session, user) == {}


async def test_rerating_same_day_applies_only_the_difference(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    headers = bearer(make_token())
    user, river = await prepare(client, db_session, headers)

    await rate(client, headers, river, 1)
    assert await refreshed_weights(db_session, user) == {"river": -0.2, "park": -0.2}

    # 같은 날 같은 경로를 4점으로 고치면 1점(−0.2)을 되돌리고 4점(+0.1)
    await rate(client, headers, river, 4)
    assert await refreshed_weights(db_session, user) == {"river": 0.1, "park": 0.1}

    # 같은 점수를 또 줘도 변화 없음
    await rate(client, headers, river, 4)
    assert await refreshed_weights(db_session, user) == {"river": 0.1, "park": 0.1}
    feedback = list((await db_session.execute(select(Feedback))).scalars())
    assert [f.rating for f in feedback] == [4]


async def test_rating_out_of_range_returns_422(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    headers = bearer(make_token())
    _, river = await prepare(client, db_session, headers)

    for rating in (0, 6):
        response = await client.post(
            "/feedback", json={"route_id": str(river.id), "rating": rating}, headers=headers
        )
        assert response.status_code == 422


async def test_hint_after_three_ratings_of_four_or_more(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    headers = bearer(make_token())
    _, river = await prepare(client, db_session, headers)
    commute = (await db_session.execute(select(Commute))).scalar_one()
    more = [make_route(commute.id, tags=[PlaceType.RIVER]) for _ in range(3)]
    db_session.add_all(more)
    await db_session.flush()

    await rate(client, headers, river, 5)
    await rate(client, headers, more[0], 4)
    await rate(client, headers, more[1], 3)  # 3점은 좋아한다고 세지 않는다
    before = await client.get("/users/me/tag-weights", headers=headers)
    await rate(client, headers, more[2], 4)
    after = await client.get("/users/me/tag-weights", headers=headers)

    assert before.json()["hint"] is None  # 4점 이상 2회
    assert after.json()["hint"] == {"tag": "river", "message": "요즘 하천길을 좋아하시네요"}


async def test_feedback_survives_route_regeneration(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    # 동선을 다시 저장해 경로를 새로 만들어도 평가 기록과 그때의 태그는 남는다
    headers = bearer(make_token())
    _, river = await prepare(client, db_session, headers)
    await rate(client, headers, river, 5)

    await db_session.delete(river)
    await db_session.flush()

    feedback = (await db_session.execute(select(Feedback))).scalar_one()
    assert feedback.route_id is None
    assert feedback.rating == 5
    assert sorted(feedback.tags) == ["park", "river"]


async def test_reset_clears_weights_but_keeps_feedback(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    headers = bearer(make_token())
    user, river = await prepare(client, db_session, headers)
    await rate(client, headers, river, 5)

    response = await client.delete("/users/me/tag-weights", headers=headers)

    assert response.status_code == 200
    assert response.json()["weights"] == {}
    assert await refreshed_weights(db_session, user) == {}
    assert len(list((await db_session.execute(select(Feedback))).scalars())) == 1


async def test_feedback_on_someone_elses_route_returns_404(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    headers = bearer(make_token())
    await prepare(client, db_session, headers)

    response = await client.post(
        "/feedback", json={"route_id": str(uuid4()), "rating": 5}, headers=headers
    )

    assert response.status_code == 404
    assert response.json() == {"message": ErrorCode.ROUTE_NOT_FOUND.message}


async def test_feedback_without_token_returns_401(client: AsyncClient) -> None:
    response = await client.post("/feedback", json={"route_id": str(uuid4()), "rating": 5})

    assert response.status_code == 401


async def test_routes_endpoint_applies_weights(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    headers = bearer(make_token())
    _, river = await prepare(client, db_session, headers)
    commute = (await db_session.execute(select(Commute))).scalar_one()
    # 하천길(river·park) 녹지 40, 공원길(park) 녹지 48.
    # 5점 5번이면 river·park 가중치 1.0 → 하천길 +20, 공원길 +10 으로 역전
    park = make_route(commute.id, green_pct=48, tags=[PlaceType.PARK])
    # 같은 날 같은 경로는 한 번만 세므로, 하천길 5개에 각각 5점
    others = [make_route(commute.id, tags=[PlaceType.RIVER, PlaceType.PARK]) for _ in range(4)]
    db_session.add_all([park, *others])
    await db_session.flush()
    plain = await client.get("/commutes/me/routes", headers=headers)
    for liked in [river, *others]:
        await rate(client, headers, liked, 5)

    boosted = await client.get("/commutes/me/routes", headers=headers)

    assert plain.json()["candidates"][0]["id"] == str(park.id)
    assert boosted.json()["candidates"][0]["id"] == str(river.id)


async def test_untagged_route_uses_its_waypoint_place_types(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    # #113: 접근로가 길어 녹지 비율이 기준 아래면 tags 가 빈다. 경유지가 공원이면 공원으로 반영한다
    headers = bearer(make_token())
    user, _ = await prepare(client, db_session, headers)
    commute = (await db_session.execute(select(Commute))).scalar_one()
    park = Place(
        name="달성공원",
        location=from_shape(Point(128.5830, 35.8740), srid=4326),
        types=[PlaceType.PARK.value],
        description="도심 서쪽의 넓은 공원",
        city=get_settings().default_city,
    )
    db_session.add(park)
    await db_session.flush()
    untagged = make_route(commute.id, green_pct=12, tags=[], spots=[park.id])
    db_session.add(untagged)
    await db_session.flush()

    body = await rate(client, headers, untagged, 5)

    assert body["weights"]["park"] == 0.2
    assert (await refreshed_weights(db_session, user))["park"] == 0.2
    feedback = (
        await db_session.execute(select(Feedback).where(Feedback.route_id == untagged.id))
    ).scalar_one()
    assert feedback.tags == ["park"]


async def test_route_without_tags_or_waypoints_keeps_weights(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    headers = bearer(make_token())
    user, _ = await prepare(client, db_session, headers)
    commute = (await db_session.execute(select(Commute))).scalar_one()
    bare = make_route(commute.id, green_pct=5, tags=[])
    db_session.add(bare)
    await db_session.flush()

    body = await rate(client, headers, bare, 5)

    assert body["rating"] == 5
    assert body["weights"] == {}
    assert await refreshed_weights(db_session, user) == {}
