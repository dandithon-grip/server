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
from app.domain.notification.service import NotificationService
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
from tests.domain.notification.test_push_tokens import FakeFcm
from tests.domain.route.test_routes import FakeTmap

USER_A = "aaaaaaaa-0000-0000-0000-000000000001"
USER_B = "bbbbbbbb-0000-0000-0000-000000000002"


def card_service(session: AsyncSession, llm: FakeLlm, fcm: FakeFcm | None = None) -> CardService:
    return CardService(
        session,
        commute_service=CommuteService(session),
        route_service=RouteService(session, FakeTmap()),  # type: ignore[arg-type]
        user_service=UserService(session),
        llm=llm,  # type: ignore[arg-type]
        kma=FakeKma(),  # type: ignore[arg-type]
        airkorea=FakeAir(),  # type: ignore[arg-type]
        notifications=NotificationService(session, fcm or FakeFcm()),  # type: ignore[arg-type]
    )


async def commute_with_candidate(
    client: AsyncClient,
    session: AsyncSession,
    token: str,
    supabase_id: str,
    leave: str,
    *,
    device: str | None = "device",
) -> None:
    """동선 + 후보 경로. device 가 있으면 알림 기기도 등록한다 (미리 만들기 대상, #65)."""
    await client.put(
        "/commutes/me", json={**walk_commute(), "leave_time": leave}, headers=bearer(token)
    )
    if device is not None:
        await client.put(
            "/users/me/push-tokens",
            json={"token": f"{device}-{supabase_id}"},
            headers=bearer(token),
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


# --- 퇴근 알림 (#63) ------------------------------------------------------------


async def test_prewarm_sends_card_notification_once_a_day(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    token = make_token(sub=USER_A)
    await commute_with_candidate(client, db_session, token, USER_A, "18:10")
    fcm = FakeFcm()
    llm = FakeLlm(choice=GOOD_CHOICE.model_copy(update={"choice": 0}))
    now = datetime.combine(datetime.now(LOCAL_TZ).date(), time(18, 2), tzinfo=LOCAL_TZ)
    service = card_service(db_session, llm, fcm)

    await service.prewarm(now)
    await service.prewarm(now)

    assert [(device, title) for device, title, _ in fcm.sent] == [
        (f"device-{USER_A}", "오늘의 샛길")
    ]
    assert fcm.sent[0][2] == GOOD_CHOICE.text
    card = (await db_session.execute(select(TodayCard))).scalar_one()
    assert card.notified_at is not None


async def test_prewarm_notifies_even_if_card_was_already_opened(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    # 오후에 앱을 열어 카드가 이미 있어도, 퇴근 10분 전 알림은 한 번 간다 (카드는 다시 안 만든다)
    token = make_token(sub=USER_A)
    await commute_with_candidate(client, db_session, token, USER_A, "18:10")
    llm = FakeLlm(choice=GOOD_CHOICE.model_copy(update={"choice": 0}))
    now = datetime.combine(datetime.now(LOCAL_TZ).date(), time(15, 0), tzinfo=LOCAL_TZ)
    service = card_service(db_session, llm, FakeFcm())
    user = (await db_session.execute(select(User).where(User.supabase_id == USER_A))).scalar_one()
    await service.get_today(user)
    fcm = FakeFcm()
    service = card_service(db_session, llm, fcm)

    await service.prewarm(now.replace(hour=18, minute=2))

    assert len(llm.calls) == 1
    assert len(fcm.sent) == 1


# --- 대상 좁히기 (#65) ----------------------------------------------------------


async def test_prewarm_skips_users_without_notification_devices(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    # 알림을 받지 않는 사용자는 앱을 열 때 만든다. 매일 LLM·날씨 호출을 쓰지 않는다
    token = make_token(sub=USER_A)
    await commute_with_candidate(client, db_session, token, USER_A, "18:10", device=None)
    llm = FakeLlm(choice=GOOD_CHOICE.model_copy(update={"choice": 0}))
    now = datetime.combine(datetime.now(LOCAL_TZ).date(), time(18, 2), tzinfo=LOCAL_TZ)

    built = await card_service(db_session, llm).prewarm(now)

    assert built == 0
    assert llm.calls == []


async def test_prewarm_always_includes_the_demo_account(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    token = make_token(sub=USER_A)
    await commute_with_candidate(client, db_session, token, USER_A, "18:10", device=None)
    await UserService(db_session).mark_demo(USER_A)
    llm = FakeLlm(choice=GOOD_CHOICE.model_copy(update={"choice": 0}))
    now = datetime.combine(datetime.now(LOCAL_TZ).date(), time(18, 2), tzinfo=LOCAL_TZ)

    built = await card_service(db_session, llm).prewarm(now)

    assert built == 1
