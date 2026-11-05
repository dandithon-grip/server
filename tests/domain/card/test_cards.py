"""F-05 오늘 카드 기대 동작.

LLM·날씨·미세먼지는 가짜로 바꾼다. 경로는 Route 행을 직접 넣는다 (TMAP 호출 없음).
"""

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from geoalchemy2.shape import from_shape
from httpx import AsyncClient
from pydantic import BaseModel
from shapely.geometry import LineString, Point
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.airkorea import AirGrade, AirQuality, get_airkorea_client
from app.clients.kma import Weather, WeatherSky, get_kma_client
from app.clients.llm import get_llm_client
from app.core.config import get_settings
from app.core.exceptions import AppException, ErrorCode
from app.domain.card.models import ReasonTag, TodayCard
from app.domain.card.prompt import CardChoice
from app.domain.commute.models import Commute
from app.domain.route.models import ActionType, LegType, Place, PlaceType, Route
from app.domain.user.models import User
from app.main import app
from tests.conftest import TokenFactory

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

    async def complete_json(
        self, prompt: str, schema: type[BaseModel], *, system: str | None = None
    ) -> BaseModel:
        self.calls.append(prompt)
        if self.error is not None:
            raise AppException(self.error)
        assert self.choice is not None
        return self.choice


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
) -> Route:
    return Route(
        commute_id=commute_id,
        leg_type=LegType.WALK_ALL,
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
    action_type=ActionType.DETOUR,
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


async def test_preference_chip_rebuilds_and_overwrites(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    headers = bearer(make_token())
    await prepare_routes(client, db_session, headers, with_candidates=True)
    llm = FakeLlm(choice=GOOD_CHOICE)
    use_fakes(llm)
    await client.get("/cards/today", headers=headers)

    response = await client.get("/cards/today", params={"preference": "short"}, headers=headers)

    assert response.status_code == 200
    assert response.json()["preference"] == "short"
    assert len(llm.calls) == 2
    assert len(await cards_in_db(db_session)) == 1


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
        action_type=ActionType.DETOUR,
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


async def test_no_candidates_is_level_5_without_llm(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    headers = bearer(make_token())
    await prepare_routes(client, db_session, headers, with_candidates=False)
    llm = FakeLlm(choice=GOOD_CHOICE)
    use_fakes(llm)

    response = await client.get("/cards/today", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["fallback_level"] == 5
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
