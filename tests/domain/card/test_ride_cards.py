"""노선별 오늘의 카드 기대 동작 (#90).

탑승 계획이 여러 개면 홈에서 "오늘 탄 노선" 을 고른다. 그 노선 기준 카드는 (사용자, 날짜, 계획) 별로
저장·캐시하고, 퇴근 전 미리 만들기는 계획별 카드도 만들어 둔다. 카드 문장은 노선명을 쓸 수 있다.
"""

from datetime import datetime, time
from uuid import uuid4

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.tmap import get_tmap_client
from app.core.timezone import LOCAL_TZ
from app.domain.card.models import TodayCard
from app.domain.card.prompt import CandidateInput, CardInput, build_prompt, names_in_input
from app.domain.route.models import PlaceType
from app.main import app
from tests.conftest import TokenFactory
from tests.domain.card.test_cards import GOOD_CHOICE, FakeLlm, bearer, use_fakes
from tests.domain.card.test_prewarm import card_service
from tests.domain.commute.test_rides import BUS_156, BUS_402, body_with_options
from tests.domain.route.test_rides import BRIDGE_NEAR_156
from tests.domain.route.test_routes import FakeTmap, place


async def two_ride_commute_with_routes(
    client: AsyncClient, db_session: AsyncSession, headers: dict[str, str], *, leave: str = "18:10"
) -> dict[str, str]:
    """402·156 두 계획 + 156 쪽 다리 후보. 계획 id → 노선 이름."""
    db_session.add(place("다리", *BRIDGE_NEAR_156, [PlaceType.BRIDGE]))
    await db_session.flush()
    app.dependency_overrides[get_tmap_client] = lambda: FakeTmap()
    body = {**body_with_options([BUS_402], [BUS_156]), "leave_time": leave}
    saved = await client.put("/commutes/me", json=body, headers=headers)
    assert saved.status_code == 200
    generated = await client.post("/commutes/me/routes", headers=headers)
    assert generated.status_code == 200
    return {ride["id"]: ride["line"] for ride in saved.json()["rides"]}


def ride_of(rides: dict[str, str], line: str) -> str:
    return next(ride_id for ride_id, name in rides.items() if name == line)


async def test_card_for_a_ride_uses_only_that_rides_routes(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    use_fakes(FakeLlm(choice=GOOD_CHOICE.model_copy(update={"choice": 0})))
    headers = bearer(make_token())
    rides = await two_ride_commute_with_routes(client, db_session, headers)
    ride_156 = ride_of(rides, "156")

    response = await client.get("/cards/today", params={"ride_id": ride_156}, headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["ride_id"] == ride_156
    assert body["fallback_level"] == 1
    # 후보·최단은 그 계획 것(도보 통근·도착 후처럼 계획이 없는 구간은 허용)
    for route in [*body["candidates"], *body["shortest"]]:
        assert route["ride_id"] in (ride_156, None)
    assert body["route"]["ride_id"] == ride_156


async def test_ride_card_is_cached_per_ride_and_default_card_stays_separate(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    llm = FakeLlm(choice=GOOD_CHOICE.model_copy(update={"choice": 0}))
    use_fakes(llm)
    headers = bearer(make_token())
    rides = await two_ride_commute_with_routes(client, db_session, headers)
    ride_156, ride_402 = ride_of(rides, "156"), ride_of(rides, "402")

    first = await client.get("/cards/today", params={"ride_id": ride_156}, headers=headers)
    second = await client.get("/cards/today", params={"ride_id": ride_156}, headers=headers)
    default = await client.get("/cards/today", headers=headers)
    other = await client.get("/cards/today", params={"ride_id": ride_402}, headers=headers)

    assert first.json()["text"] == second.json()["text"]
    assert len(llm.calls) == 3  # 156 한 번, 기본 한 번, 402 한 번. 두 번째 156 은 캐시
    assert default.json()["ride_id"] is None
    assert other.json()["ride_id"] == ride_402
    cards = list((await db_session.execute(select(TodayCard))).scalars())
    assert sorted(str(c.ride_id) for c in cards) == sorted([ride_156, ride_402, "None"])


async def test_unknown_ride_returns_404(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    use_fakes(FakeLlm(choice=GOOD_CHOICE))
    headers = bearer(make_token())
    await two_ride_commute_with_routes(client, db_session, headers)

    response = await client.get("/cards/today", params={"ride_id": str(uuid4())}, headers=headers)

    assert response.status_code == 404


async def test_prewarm_builds_a_card_per_ride_when_there_are_two(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    sub = "cccccccc-0000-0000-0000-000000000003"
    headers = bearer(make_token(sub=sub))
    await two_ride_commute_with_routes(client, db_session, headers)
    await client.put("/users/me/push-tokens", json={"token": f"device-{sub}"}, headers=headers)
    llm = FakeLlm(choice=GOOD_CHOICE.model_copy(update={"choice": 0}))
    now = datetime.combine(datetime.now(LOCAL_TZ).date(), time(18, 2), tzinfo=LOCAL_TZ)

    built = await card_service(db_session, llm).prewarm(now)

    # 기본 카드 1 + 계획별 2. 알림은 기본 카드로 한 번
    assert built == 3
    assert len(llm.calls) == 3
    cards = list((await db_session.execute(select(TodayCard))).scalars())
    assert sum(1 for c in cards if c.ride_id is None) == 1
    assert sum(1 for c in cards if c.ride_id is not None) == 2


def test_prompt_carries_the_line_name_so_the_sentence_can_say_it() -> None:
    candidate = CandidateInput(
        index=0,
        name="달성공원 샛길",
        action_type="alight_early",
        add_minutes=8,
        green_pct=30,
        event_score=0,
        score=20,
        station="대구제일고건너",
        skip_stops=1,
        line="156",
    )
    card_input = CardInput(
        candidates=[candidate], fallback_level=1, leave_time="18:10", sunset_at="17:30"
    )

    assert '"line":"156"' in build_prompt(card_input)
    assert "156" in names_in_input(card_input)
