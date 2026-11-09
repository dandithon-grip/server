"""F-05 오늘 카드 기대 동작.

LLM·날씨·미세먼지는 가짜로 바꾼다. 경로는 Route 행을 직접 넣는다 (TMAP 호출 없음).
"""

import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID

from geoalchemy2.shape import from_shape
from httpx import AsyncClient
from pydantic import BaseModel
from shapely.geometry import LineString, Point
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.airkorea import AirGrade, AirQuality, get_airkorea_client
from app.clients.fcm import get_fcm_client
from app.clients.kma import Weather, WeatherSky, get_kma_client
from app.clients.llm import LlmReply, get_llm_client
from app.core.config import get_settings
from app.core.exceptions import AppException, ErrorCode
from app.core.timezone import LOCAL_TZ
from app.domain.card.models import ReasonTag, TodayCard
from app.domain.card.prompt import CardChoice
from app.domain.commute.models import Commute, Station
from app.domain.route.models import LegType, Place, PlaceType, Route
from app.domain.route.scoring import sunset_window
from app.domain.user.models import User
from app.main import app
from tests.conftest import TokenFactory
from tests.domain.notification.test_push_tokens import FakeFcm

A = (35.8600, 128.6000)
B = (35.8600, 128.6110)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- 가짜 클라이언트 -----------------------------------------------------------


@dataclass
class FakeLlm:
    """정해진 CardChoice 를 돌려주거나 예외를 던진다. 호출을 센다."""

    choice: CardChoice | None = None
    error: ErrorCode | None = None
    calls: list[str] = field(default_factory=list)

    async def complete_json_reply(
        self, prompt: str, schema: type[BaseModel], *, system: str | None = None
    ) -> LlmReply[BaseModel]:
        self.calls.append(prompt)
        if self.error is not None:
            raise AppException(self.error)
        assert self.choice is not None
        return LlmReply(parsed=self.choice, input_tokens=1200, output_tokens=90, latency_ms=2300)


@dataclass
class FakeKma:
    weather: Weather = field(default_factory=lambda: Weather(sky=WeatherSky.CLEAR, temp_c=12))
    fail: bool = False

    async def forecast(self, lat: float, lng: float, at: datetime) -> Weather:
        if self.fail:
            raise AppException(ErrorCode.EXTERNAL_API_FAILED)
        return self.weather


@dataclass
class FakeAir:
    async def pm(self, city: str) -> AirQuality:
        return AirQuality(grade=AirGrade.GOOD, pm10=20, pm25=10)


def use_fakes(llm: FakeLlm, kma: FakeKma | None = None) -> None:
    app.dependency_overrides[get_llm_client] = lambda: llm
    app.dependency_overrides[get_kma_client] = lambda: kma or FakeKma()
    app.dependency_overrides[get_airkorea_client] = lambda: FakeAir()


# --- 데이터 준비 ----------------------------------------------------------------


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
    name: str,
    is_baseline: bool = False,
    add_minutes: float = 5,
    green_pct: float = 0,
    landmark_score: float = 0,
    tags: list[PlaceType] | None = None,
    highlights: list[str] | None = None,
    spots: list[UUID] | None = None,
    leg_type: LegType = LegType.WALK_ALL,
    name_reverse: str | None = None,
    skip_stops: int = 0,
    board_station_id: UUID | None = None,
    alight_station_id: UUID | None = None,
) -> Route:
    return Route(
        commute_id=commute_id,
        leg_type=leg_type,
        name_reverse=name_reverse,
        skip_stops=skip_stops,
        board_station_id=board_station_id,
        alight_station_id=alight_station_id,
        is_baseline=is_baseline,
        name=name,
        minutes=12 + add_minutes,
        add_minutes=add_minutes,
        polyline=from_shape(LineString([(A[1], A[0]), (B[1], B[0])]), srid=4326),
        green_pct=green_pct,
        water_score=0,
        landmark_score=landmark_score,
        sunset_ratio=0,
        tags=[t.value for t in (tags or [])],
        spots=spots or [],
        highlights=highlights or [],
    )


async def commute_of(session: AsyncSession, supabase_id: str) -> Commute:
    stmt = (
        select(Commute)
        .join(User, User.id == Commute.user_id)
        .where(User.supabase_id == supabase_id)
    )
    return (await session.execute(stmt)).scalar_one()


async def prepare_routes(
    client: AsyncClient, session: AsyncSession, headers: dict[str, str], *, with_candidates: bool
) -> list[Route]:
    """동선 저장 + Route 행 2~3개. with_candidates 면 후보가 되는 공원 경로가 들어간다."""
    await client.put("/commutes/me", json=walk_commute(), headers=headers)
    commute = await commute_of(session, "11111111-1111-1111-1111-111111111111")
    park = Place(
        name="2.28기념중앙공원",
        location=from_shape(Point(128.6050, 35.8605), srid=4326),
        types=[PlaceType.PARK.value],
        description="반월당 옆 작은 숲",
        season_tag="단풍",
        city=get_settings().default_city,
    )
    session.add(park)
    await session.flush()
    routes = [make_route(commute.id, name="최단 경로", is_baseline=True, green_pct=10)]
    if with_candidates:
        routes.append(
            make_route(
                commute.id,
                name="2.28기념중앙공원 샛길",
                add_minutes=5,
                green_pct=60,
                tags=[PlaceType.PARK],
                highlights=[park.description],
                spots=[park.id],
            )
        )
        routes.append(
            make_route(
                commute.id,
                name="경상감영공원 샛길",
                add_minutes=7,
                green_pct=45,
                tags=[PlaceType.PARK, PlaceType.TREES],
                highlights=["도심 한가운데 느티나무 그늘"],
            )
        )
    session.add_all(routes)
    await session.flush()
    return routes


async def cards_in_db(session: AsyncSession) -> list[TodayCard]:
    return list((await session.execute(select(TodayCard))).scalars())


GOOD_CHOICE = CardChoice(
    choice=1,
    reason_tag=ReasonTag.TREES,
    text="오늘은 느티나무 그늘이 좋은 날입니다. 7분 돌아가 경상감영공원 샛길로 걸어 보세요.",
    deviation="단풍보다 그늘이 더 어울리는 맑은 오후라서",
)


# --- 정상 -------------------------------------------------------------------


async def test_llm_choice_is_saved_as_today_card(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    headers = bearer(make_token())
    routes = await prepare_routes(client, db_session, headers, with_candidates=True)
    llm = FakeLlm(choice=GOOD_CHOICE)
    use_fakes(llm)

    response = await client.get("/cards/today", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["fallback_level"] == 1
    assert body["action_type"] == "detour"
    assert body["by_llm"] is True
    assert body["text"] == GOOD_CHOICE.text
    assert body["deviation"] == GOOD_CHOICE.deviation
    assert body["reason_tag"] == "trees"
    assert body["sky"] == "clear"
    assert body["air_grade"] == "good"
    assert body["route"]["name"] == "경상감영공원 샛길"  # choice=1 → 점수 2위
    assert [r["name"] for r in body["candidates"]] == ["2.28기념중앙공원 샛길", "경상감영공원 샛길"]
    assert len(llm.calls) == 1
    # 프롬프트에 입력값이 들어갔다
    assert "2.28기념중앙공원 샛길" in llm.calls[0]
    assert '"season_tags": [\\n    "단풍"' in llm.calls[0] or "단풍" in llm.calls[0]
    assert '"sunset_at"' in llm.calls[0]
    cards = await cards_in_db(db_session)
    assert len(cards) == 1
    assert cards[0].route_id == routes[2].id


async def test_second_call_same_day_uses_cache_without_llm(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    headers = bearer(make_token())
    await prepare_routes(client, db_session, headers, with_candidates=True)
    llm = FakeLlm(choice=GOOD_CHOICE)
    use_fakes(llm)

    first = await client.get("/cards/today", headers=headers)
    second = await client.get("/cards/today", headers=headers)

    assert first.status_code == second.status_code == 200
    assert first.json()["text"] == second.json()["text"]
    assert first.json()["route"]["id"] == second.json()["route"]["id"]
    assert len(llm.calls) == 1
    assert len(await cards_in_db(db_session)) == 1


async def test_preference_chip_builds_its_own_card_and_keeps_the_default(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    # 취향 칩은 (날짜, 취향)별 카드다 (#101). 기본 취향 카드를 덮어쓰지 않고, 같은 칩을 다시 누르면 캐시
    headers = bearer(make_token())
    await prepare_routes(client, db_session, headers, with_candidates=True)
    llm = FakeLlm(choice=GOOD_CHOICE)
    use_fakes(llm)
    await client.get("/cards/today", headers=headers)

    short = await client.get("/cards/today", params={"preference": "short"}, headers=headers)
    short_again = await client.get("/cards/today", params={"preference": "short"}, headers=headers)
    default_again = await client.get("/cards/today", headers=headers)

    assert short.status_code == 200
    assert short.json()["preference"] == "short"
    assert short_again.json()["text"] == short.json()["text"]
    assert default_again.json()["preference"] == "full"
    assert len(llm.calls) == 2  # 기본 1 + short 1. 두 번째 short 와 기본 재조회는 캐시
    assert len(await cards_in_db(db_session)) == 2


# --- 폴백 -------------------------------------------------------------------


async def test_llm_failure_falls_back_to_top_score_template(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    headers = bearer(make_token())
    await prepare_routes(client, db_session, headers, with_candidates=True)
    use_fakes(FakeLlm(error=ErrorCode.LLM_FAILED))

    response = await client.get("/cards/today", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["by_llm"] is False
    assert body["route"]["name"] == "2.28기념중앙공원 샛길"  # 점수 1위
    assert body["text"] == "5분 돌아가면 2.28기념중앙공원 샛길. 반월당 옆 작은 숲"
    assert body["reason_tag"] == "park"
    assert body["deviation"] is None


async def test_text_with_place_not_in_input_falls_back(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    headers = bearer(make_token())
    await prepare_routes(client, db_session, headers, with_candidates=True)
    db_session.add(
        Place(
            name="수성못",
            location=from_shape(Point(128.6179, 35.8273), srid=4326),
            types=[PlaceType.SUNSET.value],
            description="둘레 2km 산책로",
            city=get_settings().default_city,
        )
    )
    await db_session.flush()
    hallucinated = CardChoice(
        choice=0,
        reason_tag=ReasonTag.SUNSET,
        text="수성못에서 노을을 보세요. 2.28기념중앙공원 샛길로 가면 됩니다.",
        deviation=None,
    )
    use_fakes(FakeLlm(choice=hallucinated))

    response = await client.get("/cards/today", headers=headers)

    assert response.status_code == 200
    assert response.json()["by_llm"] is False
    assert "수성못" not in response.json()["text"]


async def test_choice_out_of_range_falls_back(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    headers = bearer(make_token())
    await prepare_routes(client, db_session, headers, with_candidates=True)
    use_fakes(FakeLlm(choice=GOOD_CHOICE.model_copy(update={"choice": 5})))

    response = await client.get("/cards/today", headers=headers)

    assert response.status_code == 200
    assert response.json()["by_llm"] is False


async def test_no_candidates_is_level_4_without_llm(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    headers = bearer(make_token())
    await prepare_routes(client, db_session, headers, with_candidates=False)
    llm = FakeLlm(choice=GOOD_CHOICE)
    use_fakes(llm)

    response = await client.get("/cards/today", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["fallback_level"] == 4
    assert body["action_type"] == "none"
    assert body["route"] is None
    assert "자연이 없어요" in body["text"]
    assert llm.calls == []


async def test_weather_failure_still_makes_card(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    headers = bearer(make_token())
    await prepare_routes(client, db_session, headers, with_candidates=True)
    use_fakes(FakeLlm(choice=GOOD_CHOICE), kma=FakeKma(fail=True))

    response = await client.get("/cards/today", headers=headers)

    assert response.status_code == 200
    assert response.json()["sky"] is None
    assert response.json()["by_llm"] is True


# --- 접근 -------------------------------------------------------------------


async def test_today_without_token_returns_401(client: AsyncClient) -> None:
    assert (await client.get("/cards/today")).status_code == 401


async def test_today_without_routes_returns_404(
    client: AsyncClient, make_token: TokenFactory
) -> None:
    headers = bearer(make_token())
    await client.put("/commutes/me", json=walk_commute(), headers=headers)
    use_fakes(FakeLlm(choice=GOOD_CHOICE))

    response = await client.get("/cards/today", headers=headers)

    assert response.status_code == 404
    assert response.json() == {"message": ErrorCode.ROUTES_NOT_READY.message}


async def test_demo_card_without_token(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    # 데모 계정은 시드가 is_demo 를 켠다. 여기선 로그인 사용자를 데모로 표시한다
    headers = bearer(make_token())
    await prepare_routes(client, db_session, headers, with_candidates=True)
    user = (await db_session.execute(select(User))).scalar_one()
    user.is_demo = True
    await db_session.flush()
    use_fakes(FakeLlm(choice=GOOD_CHOICE))

    response = await client.get("/cards/demo")

    assert response.status_code == 200
    assert response.json()["route"]["name"] == "경상감영공원 샛길"


async def test_demo_card_without_seed_returns_404(client: AsyncClient) -> None:
    use_fakes(FakeLlm(choice=GOOD_CHOICE))

    response = await client.get("/cards/demo")

    assert response.status_code == 404
    assert response.json() == {"message": ErrorCode.DEMO_NOT_READY.message}


async def test_place_name_inside_route_name_is_allowed(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    # 경로 이름 "2.28기념중앙공원 샛길" 안의 장소명 "2.28기념중앙공원" 은 입력에 있는 것이다
    headers = bearer(make_token())
    await prepare_routes(client, db_session, headers, with_candidates=True)
    choice = CardChoice(
        choice=0,
        reason_tag=ReasonTag.PARK,
        text="5분만 돌아가면 2.28기념중앙공원 숲을 지납니다. 오늘은 그늘이 좋은 날입니다.",
        deviation=None,
    )
    use_fakes(FakeLlm(choice=choice))

    response = await client.get("/cards/today", headers=headers)

    assert response.status_code == 200
    assert response.json()["by_llm"] is True
    assert response.json()["text"] == choice.text


async def test_weather_and_air_are_fetched_concurrently(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    """순차면 [kma 시작, kma 끝, air 시작, ...], 동시면 두 시작이 먼저 온다."""
    import asyncio

    order: list[str] = []

    class SlowKma:
        async def forecast(self, lat: float, lng: float, at: datetime) -> Weather:
            order.append("kma-start")
            await asyncio.sleep(0)
            order.append("kma-end")
            return Weather(sky=WeatherSky.CLEAR, temp_c=10)

    class SlowAir:
        async def pm(self, city: str) -> AirQuality:
            order.append("air-start")
            await asyncio.sleep(0)
            order.append("air-end")
            return AirQuality(grade=AirGrade.GOOD)

    headers = bearer(make_token())
    await prepare_routes(client, db_session, headers, with_candidates=True)
    app.dependency_overrides[get_llm_client] = lambda: FakeLlm(choice=GOOD_CHOICE)
    app.dependency_overrides[get_kma_client] = lambda: SlowKma()
    app.dependency_overrides[get_airkorea_client] = lambda: SlowAir()

    response = await client.get("/cards/today", headers=headers)

    assert response.status_code == 200
    assert order[:2] == ["kma-start", "air-start"]


# --- 정거장 건너뛰기 카드 (#51) -------------------------------------------------


async def add_station(session: AsyncSession, name: str) -> Station:
    station = Station(
        line="309",
        name=name,
        location=from_shape(Point(A[1], A[0]), srid=4326),
        order_no=0,
        external_id=f"ext-{name}",
        city=get_settings().default_city,
    )
    session.add(station)
    await session.flush()
    return station


async def test_board_late_template_names_stops_and_station(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    headers = bearer(make_token())
    await client.put("/commutes/me", json=walk_commute(), headers=headers)
    commute = await commute_of(db_session, "11111111-1111-1111-1111-111111111111")
    station = await add_station(db_session, "수성구청건너")
    db_session.add_all(
        [
            make_route(commute.id, name="최단 경로", is_baseline=True),
            # 출근 방향 "회사 쪽 2정거장 먼저 내리기" 는 퇴근 카드에서 "걸어가서 타기" (#56)
            make_route(
                commute.id,
                name="수성구청건너에서 내려 걷기",
                name_reverse="수성구청건너까지 걸어가서 타기",
                leg_type=LegType.ALIGHT_EARLY,
                skip_stops=2,
                alight_station_id=station.id,
                add_minutes=6,
                green_pct=40,
            ),
        ]
    )
    await db_session.flush()
    use_fakes(FakeLlm(error=ErrorCode.LLM_FAILED))

    response = await client.get("/cards/today", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert (body["fallback_level"], body["action_type"]) == (1, "board_late")
    assert body["text"].startswith("2정거장 걸어가서 수성구청건너에서 타면 +6분")


async def test_card_action_follows_chosen_candidate_not_llm_label(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    # 1단계에 걷기·먼저 내리기가 섞이므로 행동 유형은 고른 후보의 구간에서 정한다
    headers = bearer(make_token())
    await client.put("/commutes/me", json=walk_commute(), headers=headers)
    commute = await commute_of(db_session, "11111111-1111-1111-1111-111111111111")
    station = await add_station(db_session, "삼덕교회")
    db_session.add_all(
        [
            make_route(commute.id, name="최단 경로", is_baseline=True),
            # 출근 방향 "집 쪽 걸어가서 타기" 는 퇴근 카드에서 "먼저 내리기" (#56)
            make_route(
                commute.id,
                name="삼덕교회까지 걸어가서 타기",
                name_reverse="삼덕교회에서 내려 걷기",
                leg_type=LegType.BOARD_LATE,
                skip_stops=1,
                board_station_id=station.id,
                add_minutes=3,
                green_pct=50,
            ),
        ]
    )
    await db_session.flush()
    mislabeled = CardChoice(
        choice=0,
        reason_tag=ReasonTag.PARK,
        text="삼덕교회에서 내려 걸어 보세요. 오늘은 공기가 좋습니다.",
        deviation=None,
    )
    use_fakes(FakeLlm(choice=mislabeled))

    response = await client.get("/cards/today", headers=headers)

    assert response.status_code == 200
    assert response.json()["by_llm"] is True
    assert response.json()["action_type"] == "alight_early"


# --- 토큰 줄이기 · 사용량 (#59) -------------------------------------------------


def test_llm_output_schema_does_not_ask_for_action_type() -> None:
    # 행동 유형은 서버가 고른 후보의 구간으로 정한다. LLM 이 낼 필요가 없다 (스키마 353 토큰)
    assert "action_type" not in CardChoice.model_fields


async def test_card_saves_llm_token_usage_and_latency(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    headers = bearer(make_token())
    await prepare_routes(client, db_session, headers, with_candidates=True)
    use_fakes(FakeLlm(choice=GOOD_CHOICE))

    await client.get("/cards/today", headers=headers)

    card = (await cards_in_db(db_session))[0]
    assert (card.llm_input_tokens, card.llm_output_tokens, card.llm_ms) == (1200, 90, 2300)


async def test_prompt_is_compact_json_without_route_ids(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    headers = bearer(make_token())
    routes = await prepare_routes(client, db_session, headers, with_candidates=True)
    user = (await db_session.execute(select(User))).scalar_one()
    # 어제 경상감영공원 샛길을 추천했다
    db_session.add(
        TodayCard(
            user_id=user.id,
            date=date.today() - timedelta(days=1),
            route_id=routes[2].id,
            action_type="detour",
            fallback_level=1,
            reason_tag="trees",
            text="어제 카드",
            preference="full",
            sunset_at=datetime.now(UTC),
            by_llm=True,
        )
    )
    await db_session.flush()
    llm = FakeLlm(choice=GOOD_CHOICE)
    use_fakes(llm)

    await client.get("/cards/today", headers=headers)

    prompt = llm.calls[0]
    payload = json.loads(prompt[prompt.index("{") :])
    assert "\n " not in prompt  # 들여쓰기 없음
    assert all(str(r.id) not in prompt for r in routes)  # 경로 UUID 를 넣지 않는다
    assert "recent_route_ids" not in payload
    assert [c.get("recent", False) for c in payload["candidates"]] == [False, True]
    assert all("leg_type" not in c and "station" not in c for c in payload["candidates"])


async def test_prompt_marks_open_sunset_window_for_the_llm(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    # 노을 창(일몰 ±30분·맑음)은 서버가 계산해 넣는다. LLM 이 시각 계산을 하지 않게 (#61 평가)
    headers = bearer(make_token())
    await prepare_routes(client, db_session, headers, with_candidates=True)
    today = datetime.now(LOCAL_TZ).date()
    noon = datetime.combine(today, time(12, 0), tzinfo=LOCAL_TZ)
    sunset = sunset_window(noon, lat=A[0], lng=A[1], is_clear=True).sunset_at.astimezone(LOCAL_TZ)
    leave = (sunset + timedelta(minutes=5)).strftime("%H:%M")
    await client.put("/commutes/me", json={**walk_commute(), "leave_time": leave}, headers=headers)
    llm = FakeLlm(choice=GOOD_CHOICE)
    use_fakes(llm)

    await client.get("/cards/today", headers=headers)

    assert '"sunset_window_open":true' in llm.calls[0]


async def test_prompt_omits_closed_sunset_window(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    headers = bearer(make_token())
    await prepare_routes(client, db_session, headers, with_candidates=True)
    llm = FakeLlm(choice=GOOD_CHOICE)
    use_fakes(llm, kma=FakeKma(weather=Weather(sky=WeatherSky.RAIN, temp_c=10)))

    await client.get("/cards/today", headers=headers)

    assert "sunset_window_open" not in llm.calls[0]


# --- 시연용: 데모 다시 만들기 · 오늘 카드 알림 (#65) ------------------------------


async def test_demo_refresh_rebuilds_card_after_cooldown(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    await prepare_routes(client, db_session, bearer(make_token()), with_candidates=True)
    user = (await db_session.execute(select(User))).scalar_one()
    user.is_demo = True
    await db_session.flush()
    llm = FakeLlm(choice=GOOD_CHOICE)
    use_fakes(llm)
    await client.get("/cards/demo")
    card = (await cards_in_db(db_session))[0]
    card.updated_at = datetime.now(UTC) - timedelta(minutes=2)
    await db_session.flush()

    response = await client.get("/cards/demo", params={"refresh": "true"})

    assert response.status_code == 200
    assert len(llm.calls) == 2


async def test_demo_refresh_within_cooldown_returns_cached_card(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    # 공개 API 라 연타로 LLM 을 소진하지 못하게 60초 안에는 다시 만들지 않는다
    await prepare_routes(client, db_session, bearer(make_token()), with_candidates=True)
    user = (await db_session.execute(select(User))).scalar_one()
    user.is_demo = True
    await db_session.flush()
    llm = FakeLlm(choice=GOOD_CHOICE)
    use_fakes(llm)
    await client.get("/cards/demo")

    await client.get("/cards/demo", params={"refresh": "true"})

    assert len(llm.calls) == 1


async def test_push_today_card_sends_its_text_to_my_devices(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    headers = bearer(make_token())
    await prepare_routes(client, db_session, headers, with_candidates=True)
    use_fakes(FakeLlm(choice=GOOD_CHOICE))
    fcm = FakeFcm()
    app.dependency_overrides[get_fcm_client] = lambda: fcm
    await client.put("/users/me/push-tokens", json={"token": "phone"}, headers=headers)

    response = await client.post("/cards/today/push", headers=headers)

    assert response.status_code == 200
    assert response.json() == {"sent": 1}
    assert fcm.sent == [("phone", "오늘의 샛길", GOOD_CHOICE.text)]


async def test_push_today_card_without_fcm_returns_503(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    headers = bearer(make_token())
    await prepare_routes(client, db_session, headers, with_candidates=True)
    use_fakes(FakeLlm(choice=GOOD_CHOICE))
    app.dependency_overrides[get_fcm_client] = lambda: FakeFcm(configured=False)

    response = await client.post("/cards/today/push", headers=headers)

    assert response.status_code == 503


# --- 템플릿 길이 (#68) -----------------------------------------------------------


async def test_template_text_fits_the_80_char_limit_with_long_names(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    # 11/8 운영: LLM 크레딧 부족 → 템플릿 → 120자에서 잘라 80자 스키마를 넘겨 500
    headers = bearer(make_token())
    await client.put("/commutes/me", json=walk_commute(), headers=headers)
    commute = await commute_of(db_session, "11111111-1111-1111-1111-111111111111")
    station = await add_station(db_session, "방천시장(김광석길)건너")
    db_session.add_all(
        [
            make_route(commute.id, name="최단 경로", is_baseline=True),
            # 출근 "3정거장 먼저 내리기" → 퇴근 카드 "3정거장 걸어가서 타기" (정류장 이름 포함)
            make_route(
                commute.id,
                name="국채보상운동기념공원 북동쪽 입구 샛길 (3정거장 먼저)",
                name_reverse="국채보상운동기념공원 북동쪽 입구 샛길 (3정거장 걸어가서)",
                leg_type=LegType.ALIGHT_EARLY,
                skip_stops=3,
                alight_station_id=station.id,
                add_minutes=12,
                green_pct=60,
                highlights=["동성로 옆 넓은 잔디와 느티나무길. 퇴근길에 가로질러 걷기 좋다"],
            ),
        ]
    )
    await db_session.flush()
    use_fakes(FakeLlm(error=ErrorCode.EXTERNAL_API_FAILED))

    response = await client.get("/cards/today", headers=headers)

    assert response.status_code == 200
    assert response.json()["by_llm"] is False
    assert len(response.json()["text"]) <= 80
