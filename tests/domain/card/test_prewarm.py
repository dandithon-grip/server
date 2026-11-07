"""퇴근 전 카드 미리 만들기 기대 동작 (#59).

알림 시각에 카드를 미리 만들어 두면 사용자는 캐시된 카드를 1~2초 안에 받는다.
"""

from datetime import datetime, time

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import LOCAL_TZ
from app.domain.card.models import TodayCard
from app.domain.card.service import CardService
from app.domain.commute.service import CommuteService
from app.domain.route.service import RouteService
from app.domain.user.models import User
from app.domain.user.service import UserService
from tests.conftest import TokenFactory
from tests.domain.card.test_cards import (
    GOOD_CHOICE,
    FakeAir,
    FakeKma,
    FakeLlm,
    bearer,
    commute_of,
    make_route,
    walk_commute,
)
from tests.domain.route.test_routes import FakeTmap

USER_A = "aaaaaaaa-0000-0000-0000-000000000001"
USER_B = "bbbbbbbb-0000-0000-0000-000000000002"


def card_service(session: AsyncSession, llm: FakeLlm) -> CardService:
    return CardService(
        session,
        commute_service=CommuteService(session),
        route_service=RouteService(session, FakeTmap()),  # type: ignore[arg-type]
        user_service=UserService(session),
        llm=llm,  # type: ignore[arg-type]
        kma=FakeKma(),  # type: ignore[arg-type]
        airkorea=FakeAir(),  # type: ignore[arg-type]
    )


async def commute_with_candidate(
    client: AsyncClient, session: AsyncSession, token: str, supabase_id: str, leave: str
) -> None:
    await client.put(
        "/commutes/me", json={**walk_commute(), "leave_time": leave}, headers=bearer(token)
    )
    commute = await commute_of(session, supabase_id)
    session.add_all(
        [
            make_route(commute.id, name="최단 경로", is_baseline=True, green_pct=10),
            make_route(commute.id, name="공원 샛길", add_minutes=5, green_pct=60),
        ]
    )
    await session.flush()


async def cards_of(session: AsyncSession) -> list[str]:
    stmt = select(User.supabase_id).join(TodayCard, TodayCard.user_id == User.id)
    return list((await session.execute(stmt)).scalars())


async def test_prewarm_builds_only_commutes_leaving_within_10_minutes(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    await commute_with_candidate(client, db_session, make_token(sub=USER_A), USER_A, "18:10")
    await commute_with_candidate(client, db_session, make_token(sub=USER_B), USER_B, "20:00")
    llm = FakeLlm(choice=GOOD_CHOICE.model_copy(update={"choice": 0}))
    now = datetime.combine(datetime.now(LOCAL_TZ).date(), time(18, 2), tzinfo=LOCAL_TZ)

    built = await card_service(db_session, llm).prewarm(now)

    assert built == 1
    assert await cards_of(db_session) == [USER_A]


async def test_prewarm_twice_does_not_call_llm_again(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    await commute_with_candidate(client, db_session, make_token(sub=USER_A), USER_A, "18:10")
    llm = FakeLlm(choice=GOOD_CHOICE.model_copy(update={"choice": 0}))
    now = datetime.combine(datetime.now(LOCAL_TZ).date(), time(18, 2), tzinfo=LOCAL_TZ)
    service = card_service(db_session, llm)

    await service.prewarm(now)
    second = await service.prewarm(now)

    assert second == 0
    assert len(llm.calls) == 1


async def test_prewarm_skips_commute_without_routes(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    await client.put(
        "/commutes/me", json={**walk_commute(), "leave_time": "18:10"}, headers=bearer(make_token())
    )
    now = datetime.combine(datetime.now(LOCAL_TZ).date(), time(18, 2), tzinfo=LOCAL_TZ)

    built = await card_service(db_session, FakeLlm(choice=GOOD_CHOICE)).prewarm(now)

    assert built == 0
