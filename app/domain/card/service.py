"""F-05 오늘 카드.

서버가 후보 ≤3개와 점수, 대체 단계, 날씨를 만들고 LLM 1회가 선택·이유·문장을 쓴다.
LLM 실패·검증 실패는 점수 1위 + 템플릿으로 폴백한다. 하루 1장을 캐시한다.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.airkorea import AirKoreaClient, AirQuality, get_airkorea_client
from app.clients.kma import KmaClient, Weather, WeatherSky, get_kma_client
from app.clients.llm import LlmClient, get_llm_client
from app.core.database import get_db
from app.core.exceptions import AppException, ErrorCode
from app.domain.card.models import ReasonTag, TodayCard
from app.domain.card.prompt import (
    SYSTEM_PROMPT,
    CandidateInput,
    CardChoice,
    CardInput,
    build_prompt,
    format_time,
)
from app.domain.commute.models import Commute, Preference
from app.domain.commute.service import CommuteService, get_commute_service
from app.domain.feedback.service import top_tags
from app.domain.route.fallback import FallbackResult, decide_fallback
from app.domain.route.models import ActionType, LegType, PlaceType, Route
from app.domain.route.scoring import sunset_window
from app.domain.route.service import RankedRoute, RouteService, get_route_service
from app.domain.user.models import User
from app.domain.user.service import UserService, get_user_service

logger = logging.getLogger(__name__)

LOCAL_TZ = ZoneInfo("Asia/Seoul")
RECENT_DAYS = 3
NO_NATURE_TEXT = "오늘 동선엔 자연이 없어요. 내일 다시 찾아볼게요."

LEG_ACTION = {
    LegType.WALK_ALL: ActionType.DETOUR,
    LegType.FRONT_WALK: ActionType.DETOUR,
    LegType.BACK_WALK: ActionType.DETOUR,
    LegType.ALIGHT_EARLY: ActionType.ALIGHT_EARLY,
    LegType.AFTER_ARRIVAL: ActionType.AFTER_ARRIVAL,
}


@dataclass(frozen=True)
class CardContext:
    """카드 한 장을 만들기 위해 모은 재료."""

    commute: Commute
    routes: list[Route]
    decided: FallbackResult
    at: datetime
    sunset_at: datetime
    weather: Weather
    air: AirQuality


@dataclass(frozen=True)
class CardResult:
    card: TodayCard
    chosen: RankedRoute | None
    candidates: list[RankedRoute]
    shortest: list[RankedRoute]


class CardService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        commute_service: CommuteService,
        route_service: RouteService,
        user_service: UserService,
        llm: LlmClient,
        kma: KmaClient,
        airkorea: AirKoreaClient,
    ) -> None:
        self._session = session
        self._commutes = commute_service
        self._routes = route_service
        self._users = user_service
        self._llm = llm
        self._kma = kma
        self._airkorea = airkorea

    async def get_today(self, user: User, *, preference: Preference | None = None) -> CardResult:
        """
        오늘의 카드를 돌려준다. 이미 있으면 그대로, 취향 칩(preference)이 오면 다시 만든다.

        :param user: 로그인 사용자
        :param preference: 취향 칩. None 이면 동선의 기본 취향, 캐시가 있으면 캐시 반환
        :raises AppException: COMMUTE_NOT_FOUND · ROUTES_NOT_READY
        """
        # 1. 동선·경로
        commute = await self._commutes.get_commute(user.id)
        routes = await self._routes.list_routes(commute.id)
        if not routes:
            raise AppException(ErrorCode.ROUTES_NOT_READY)
        today = datetime.now(LOCAL_TZ).date()

        # 2. 오늘 카드 캐시. 취향 칩이면 건너뛰고 다시 만든다
        if preference is None:
            cached = await self._find_card(user.id, today)
            if cached is not None:
                return await self._result_from_card(cached, commute, routes, user.tag_weights)

        # 3. 새로 만들어 저장
        return await self._build_and_save(
            user, commute, routes, today, preference or commute.preference
        )

    async def get_demo(self) -> CardResult:
        """
        둘러보기용 데모 계정의 오늘 카드. 토큰 없이 조회한다.

        :raises AppException: DEMO_NOT_READY - 시드가 안 돌았거나 동선·경로가 없음
        """
        # 1. 데모 계정·동선·경로
        user = await self._users.find_demo_user()
        commute = await self._commutes.get_commute_by_user(user.id) if user else None
        if user is None or commute is None:
            raise AppException(ErrorCode.DEMO_NOT_READY)
        routes = await self._routes.list_routes(commute.id)
        if not routes:
            raise AppException(ErrorCode.DEMO_NOT_READY)

        # 2. 캐시 또는 생성
        today = datetime.now(LOCAL_TZ).date()
        cached = await self._find_card(user.id, today)
        if cached is not None:
            return await self._result_from_card(cached, commute, routes, user.tag_weights)
        return await self._build_and_save(user, commute, routes, today, commute.preference)

    # --- 조립 ----------------------------------------------------------------

    async def _build_and_save(
        self, user: User, commute: Commute, routes: list[Route], today: date, preference: Preference
    ) -> CardResult:
        # 1. 오늘의 조건: 걷는 시각·일몰·날씨·미세먼지 (날씨는 실패해도 None 으로 진행)
        at = datetime.combine(today, commute.leave_time, tzinfo=LOCAL_TZ)
        weather, air = await self._weather(commute, at, user.city)
        is_clear = weather.sky == WeatherSky.CLEAR if weather.sky is not None else None
        window = sunset_window(at, lat=commute.from_lat, lng=commute.from_lng, is_clear=is_clear)

        # 2. 후보·대체 단계 (DB 미접근)
        ranked = self._routes.rank(
            routes, preference=preference, at=at, is_clear=is_clear, tag_weights=user.tag_weights
        )
        decided = decide_fallback(
            ranked, transport_mode=commute.transport_mode, at=at, is_clear=is_clear
        )
        context = CardContext(commute, routes, decided, at, window.sunset_at, weather, air)

        # 3. 후보가 있으면 LLM 이 고르고, 없으면(3·5단계) 템플릿
        choice: CardChoice | None = None
        by_llm = False
        if decided.candidates:
            choice = await self._ask_llm(context, user)
            by_llm = choice is not None
        if choice is None:
            choice = self._template_choice(context)

        # 4. 저장 (user_id, date) upsert
        chosen = decided.candidates[choice.choice] if decided.candidates else None
        values = {
            "user_id": user.id,
            "date": today,
            "route_id": chosen.route.id if chosen else None,
            "action_type": str(choice.action_type),
            "fallback_level": decided.level,
            "reason_tag": str(choice.reason_tag),
            "text": choice.text,
            "deviation": choice.deviation,
            "preference": str(preference),
            "sunset_at": window.sunset_at,
            "sky": str(weather.sky) if weather.sky else None,
            "air_grade": str(air.grade) if air.grade else None,
            "by_llm": by_llm,
        }
        stmt = insert(TodayCard).values(**values)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_today_cards_user_id_date",
            set_={k: v for k, v in values.items() if k not in ("user_id", "date")},
        )
        await self._session.execute(stmt)
        card = await self._find_card(user.id, today)
        await self._session.commit()
        assert card is not None
        return CardResult(card, chosen, decided.candidates, decided.shortest)

    async def _ask_llm(self, context: CardContext, user: User) -> CardChoice | None:
        """LLM 1회. 실패·검증 실패면 None (폴백은 호출하는 쪽)."""
        # 1. 입력 조립
        card_input = await self._card_input(context, user)
        allowed_names = self._names_in_input(context)

        # 2. 호출
        try:
            choice = await self._llm.complete_json(
                build_prompt(card_input), CardChoice, system=SYSTEM_PROMPT
            )
        except AppException as exc:
            logger.warning("카드 LLM 실패, 템플릿으로 폴백: %s", exc.code.name)
            return None

        # 3. 검증: 후보 범위, 입력에 없는 장소·정류장 이름
        if not 0 <= choice.choice < len(context.decided.candidates):
            logger.warning("카드 LLM choice 범위 밖: %s", choice.choice)
            return None
        known = await self._routes.list_place_names(user.city)
        known |= await self._commutes.list_station_names(user.city)
        # 입력 문자열(경로 이름·설명) 어디에든 들어 있는 이름은 입력에 있는 것이다
        # (예: 경로 "국채보상운동기념공원 북쪽 입구 샛길" 에 대해 "국채보상운동기념공원")
        allowed_text = " ".join(allowed_names)
        foreign = [
            name for name in known if name and name in choice.text and name not in allowed_text
        ]
        if foreign:
            logger.warning("카드 LLM 문장에 입력에 없는 장소: %s", foreign)
            return None
        return choice

    async def _card_input(self, context: CardContext, user: User) -> CardInput:
        candidates: list[CandidateInput] = []
        for index, ranked in enumerate(context.decided.candidates):
            places = await self._routes.list_places(ranked.route.spots)
            station = (
                await self._commutes.get_station(ranked.route.alight_station_id)
                if ranked.route.alight_station_id
                else None
            )
            candidates.append(
                CandidateInput(
                    index=index,
                    route_id=str(ranked.route.id),
                    name=ranked.route.name,
                    leg_type=str(ranked.route.leg_type),
                    action_type=str(LEG_ACTION[LegType(ranked.route.leg_type)]),
                    # 문장에 "8.0분", "37.0%" 가 그대로 나오지 않게 정수로 넘긴다
                    add_minutes=round(ranked.route.add_minutes),
                    green_pct=round(ranked.route.green_pct),
                    event_score=round(ranked.event_score),
                    score=round(ranked.score),
                    tags=[str(tag) for tag in ranked.tags],
                    highlights=ranked.route.highlights,
                    season_tags=[p.season_tag for p in places if p.season_tag],
                    alight_station=station.name if station else None,
                )
            )
        recent = await self._recent_route_ids(user.id, context.at.date())
        return CardInput(
            candidates=candidates,
            fallback_level=context.decided.level,
            leave_time=format_time(context.at),
            sunset_at=format_time(context.sunset_at.astimezone(LOCAL_TZ)),
            sky=str(context.weather.sky) if context.weather.sky else None,
            air_grade=str(context.air.grade) if context.air.grade else None,
            top_tag_weights=top_tags(user.tag_weights),
            recent_route_ids=[str(route_id) for route_id in recent],
        )

    def _names_in_input(self, context: CardContext) -> set[str]:
        """입력에 등장한 장소·정류장 이름. LLM 문장에 이 밖의 이름이 있으면 폴백."""
        names: set[str] = set()
        for ranked in context.decided.candidates:
            names.add(ranked.route.name)
            names.update(ranked.route.highlights)
        return names

    def _template_choice(self, context: CardContext) -> CardChoice:
        """규칙 기반 문장. LLM 없이 점수 1위 + 템플릿."""
        decided = context.decided
        if decided.candidates:
            top = decided.candidates[0]
            route = top.route
            place = route.highlights[0] if route.highlights else route.name
            minutes = int(round(route.add_minutes))
            action = LEG_ACTION[LegType(route.leg_type)]
            if action == ActionType.ALIGHT_EARLY:
                text = f"한 정거장 먼저 내리면 +{minutes}분, {route.name}. {place}"
            elif action == ActionType.AFTER_ARRIVAL:
                text = f"도착 후 {minutes}분 걸으면 {route.name}. {place}"
            else:
                text = f"{minutes}분 돌아가면 {route.name}. {place}"
            return CardChoice(
                choice=0,
                action_type=action,
                reason_tag=_reason_from_tags(top.tags),
                text=text[:120],
                deviation=None,
            )
        if decided.level == 3:
            if decided.action_type == ActionType.DELAY:
                text = "10분만 늦추면 퇴근길 서쪽 하늘에 노을이 걸립니다. 오늘은 맑습니다."
                tag = ReasonTag.SUNSET
            else:
                text = "오늘 최단 경로에도 물가와 다리가 있습니다. 지나갈 때 고개를 돌려 보세요."
                tag = (
                    _reason_from_tags(decided.shortest[0].tags)
                    if decided.shortest
                    else ReasonTag.RIVER
                )
            return CardChoice(
                choice=0, action_type=decided.action_type, reason_tag=tag, text=text, deviation=None
            )
        return CardChoice(
            choice=0,
            action_type=ActionType.NONE,
            reason_tag=ReasonTag.SEASON,
            text=NO_NATURE_TEXT,
            deviation=None,
        )

    # --- 조회 ----------------------------------------------------------------

    async def _weather(
        self, commute: Commute, at: datetime, city: str
    ) -> tuple[Weather, AirQuality]:
        """날씨·미세먼지를 동시에 받는다. 실패는 빈 값으로 (카드는 날씨 없이도 나온다)."""
        return await asyncio.gather(self._forecast(commute, at), self._air_quality(city))

    async def _forecast(self, commute: Commute, at: datetime) -> Weather:
        try:
            return await self._kma.forecast(commute.from_lat, commute.from_lng, at)
        except AppException as exc:
            logger.warning("기상청 실패, 날씨 없이 진행: %s", exc.code.name)
            return Weather()

    async def _air_quality(self, city: str) -> AirQuality:
        try:
            return await self._airkorea.pm(city)
        except AppException as exc:
            logger.warning("에어코리아 실패, 미세먼지 없이 진행: %s", exc.code.name)
            return AirQuality()

    async def _find_card(self, user_id: UUID, today: date) -> TodayCard | None:
        stmt = select(TodayCard).where(TodayCard.user_id == user_id, TodayCard.date == today)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def _recent_route_ids(self, user_id: UUID, today: date) -> list[UUID]:
        """최근 3일 추천 경로 (오늘 제외). LLM 이 같은 길을 연달아 고르지 않게."""
        stmt = (
            select(TodayCard.route_id)
            .where(TodayCard.user_id == user_id, TodayCard.date < today)
            .order_by(TodayCard.date.desc())
            .limit(RECENT_DAYS)
        )
        return [route_id for route_id in (await self._session.execute(stmt)).scalars() if route_id]

    async def _result_from_card(
        self,
        card: TodayCard,
        commute: Commute,
        routes: list[Route],
        user_weights: dict[str, float],
    ) -> CardResult:
        """캐시된 카드에 오늘의 후보 목록을 붙인다 (LLM·날씨 재호출 없음)."""
        at = datetime.combine(card.date, commute.leave_time, tzinfo=LOCAL_TZ)
        is_clear = card.sky == WeatherSky.CLEAR.value if card.sky else None
        ranked = self._routes.rank(
            routes,
            preference=Preference(card.preference),
            at=at,
            is_clear=is_clear,
            tag_weights=user_weights,
        )
        decided = decide_fallback(
            ranked, transport_mode=commute.transport_mode, at=at, is_clear=is_clear
        )
        chosen = next((r for r in ranked.eligible if r.route.id == card.route_id), None)
        return CardResult(card, chosen, decided.candidates, decided.shortest)


def _reason_from_tags(tags: list[PlaceType]) -> ReasonTag:
    order = [
        (PlaceType.SUNSET, ReasonTag.SUNSET),
        (PlaceType.RIVER, ReasonTag.RIVER),
        (PlaceType.TREES, ReasonTag.TREES),
        (PlaceType.PARK, ReasonTag.PARK),
        (PlaceType.BRIDGE, ReasonTag.RIVER),
        (PlaceType.VIEW, ReasonTag.SUNSET),
    ]
    for place_type, reason in order:
        if place_type in tags:
            return reason
    return ReasonTag.PARK


def get_card_service(
    session: AsyncSession = Depends(get_db),
    commute_service: CommuteService = Depends(get_commute_service),
    route_service: RouteService = Depends(get_route_service),
    user_service: UserService = Depends(get_user_service),
    llm: LlmClient = Depends(get_llm_client),
    kma: KmaClient = Depends(get_kma_client),
    airkorea: AirKoreaClient = Depends(get_airkorea_client),
) -> CardService:
    return CardService(
        session,
        commute_service=commute_service,
        route_service=route_service,
        user_service=user_service,
        llm=llm,
        kma=kma,
        airkorea=airkorea,
    )
