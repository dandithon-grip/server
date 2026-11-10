"""F-05 오늘 카드.

서버가 후보 ≤3개와 점수, 대체 단계, 날씨를 만들고 LLM 1회가 선택·이유·문장을 쓴다.
LLM 실패·검증 실패는 점수 1위 + 템플릿으로 폴백한다. 하루 1장을 캐시한다.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.airkorea import AirKoreaClient, AirQuality, get_airkorea_client
from app.clients.fcm import get_fcm_client
from app.clients.kma import KmaClient, Weather, WeatherSky, get_kma_client
from app.clients.llm import LlmClient, LlmReply, get_llm_client
from app.clients.tmap import get_tmap_client
from app.core.database import get_db, session_factory
from app.core.exceptions import AppException, ErrorCode
from app.domain.card.models import ReasonTag, TodayCard
from app.domain.card.prompt import (
    SYSTEM_PROMPT,
    TEXT_MAX_CHARS,
    CandidateInput,
    CardChoice,
    CardInput,
    build_prompt,
    format_time,
    reject_reason,
)
from app.domain.commute.models import Commute, CommuteRide, Preference
from app.domain.commute.service import CommuteService, get_commute_service
from app.domain.feedback.service import top_tags
from app.domain.notification.service import (
    CARD_TITLE,
    NotificationService,
    get_notification_service,
)
from app.domain.route.fallback import FallbackResult, action_of, decide_fallback
from app.domain.route.journey import Segment
from app.domain.route.models import ActionType, Direction, PlaceType, Route
from app.domain.route.scoring import sunset_window
from app.domain.route.service import RankedRoute, RouteService, get_route_service
from app.domain.user.models import User
from app.domain.user.service import UserService, get_user_service

logger = logging.getLogger(__name__)

LOCAL_TZ = ZoneInfo("Asia/Seoul")
RECENT_DAYS = 3
# 퇴근 몇 분 전에 카드를 미리 만들지 (#59). 알림도 이 시각에 나간다
PREWARM_MINUTES = 10
# 데모 카드 다시 만들기 간격 (#65). 공개 API 라 연타로 LLM 을 소진하지 못하게
DEMO_REFRESH_COOLDOWN = timedelta(seconds=60)
NO_NATURE_TEXT = "오늘 동선엔 자연이 없어요. 내일 다시 찾아볼게요."


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
    sunset_window_open: bool = False


@dataclass(frozen=True)
class CardResult:
    card: TodayCard
    chosen: RankedRoute | None
    candidates: list[RankedRoute]
    shortest: list[RankedRoute]
    journeys: dict[UUID, list[Segment]] = field(default_factory=dict)  # Route id → 전체 여정 (#77)
    rides: list[CommuteRide] = field(default_factory=list)  # 탑승 계획들 (#88)


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
        notifications: NotificationService | None = None,
    ) -> None:
        self._session = session
        self._commutes = commute_service
        self._routes = route_service
        self._users = user_service
        self._llm = llm
        self._kma = kma
        self._airkorea = airkorea
        self._notifications = notifications

    async def get_today(
        self,
        user: User,
        *,
        preference: Preference | None = None,
        ride_id: UUID | None = None,
    ) -> CardResult:
        """
        오늘의 카드를 돌려준다. 이미 있으면 그대로, 취향 칩(preference)이 오면 다시 만든다.

        :param user: 로그인 사용자
        :param preference: 취향 칩. None 이면 동선의 기본 취향, 캐시가 있으면 캐시 반환
        :param ride_id: 오늘 탄 노선의 탑승 계획 (#90). None 이면 전체 계획을 합친 기본 카드.
            계획별 카드는 (사용자, 날짜, 계획) 로 따로 캐시한다
        :raises AppException: COMMUTE_NOT_FOUND · ROUTES_NOT_READY · RIDE_NOT_FOUND
        """
        # 1. 동선·경로. 탑승 계획은 이 동선의 것이어야 한다
        commute = await self._commutes.get_commute(user.id)
        if ride_id is not None and ride_id not in {ride.id for ride in commute.rides}:
            raise AppException(ErrorCode.RIDE_NOT_FOUND)
        routes = await self._routes.list_routes(commute.id)
        if not routes:
            raise AppException(ErrorCode.ROUTES_NOT_READY)
        today = datetime.now(LOCAL_TZ).date()

        # 2. 오늘 카드 캐시는 (날짜, 계획, 취향) 별 (#101). 취향 칩도 한 번 만들면 다시 안 만든다
        chosen_preference = preference or Preference(commute.preference)
        cached = await self._find_card(user.id, today, ride_id, chosen_preference)
        if cached is not None:
            return await self._result_from_card(cached, commute, routes, user)

        # 3. 새로 만들어 저장
        return await self._build_and_save(
            user, commute, routes, today, chosen_preference, ride_id=ride_id
        )

    async def prewarm(self, now: datetime) -> int:
        """
        퇴근 PREWARM_MINUTES 안에 드는 동선의 오늘 카드를 미리 만들고 알림을 보낸다 (#59 #63).

        알림을 눌렀을 때 LLM·날씨를 기다리지 않고 캐시된 카드를 바로 받게 하려는 것이다.
        카드가 이미 있으면 다시 만들지 않고, 알림은 하루 한 번만 보낸다 (notified_at).
        경로가 없는 동선은 건너뛴다. 한 사용자의 실패가 나머지를 막지 않는다.

        :param now: 현재 시각 (tz-aware)
        :return: 새로 만든 카드 수
        """
        # 1. 곧 퇴근하는 동선 중 알림을 받을 사용자와 데모 계정만 (#65).
        #    나머지는 앱을 열 때 만든다. 매일 LLM·날씨 호출을 쓰지 않으려는 것이다
        local = now.astimezone(LOCAL_TZ)
        until = local + timedelta(minutes=PREWARM_MINUTES)
        leaving = await self._commutes.list_leaving_between(local.time(), until.time())
        targets = await self._prewarm_targets([c.user_id for c in leaving])
        commutes = [c for c in leaving if c.user_id in targets]

        built = 0
        for commute in commutes:
            # 2. 카드가 없으면 만든다
            card = await self._find_card(commute.user_id, local.date())
            if card is None:
                user = await self._users.get_user(commute.user_id)
                routes = await self._routes.list_routes(commute.id)
                if user is None or not routes:
                    continue
                try:
                    result = await self._build_and_save(
                        user, commute, routes, local.date(), Preference(commute.preference)
                    )
                except AppException as exc:
                    logger.warning("카드 미리 만들기 실패, 다음 사용자로: %s", exc.code.name)
                    await self._session.rollback()
                    continue
                card = result.card
                built += 1

            # 3. 탑승 계획이 2개 이상이면 계획별 카드도 미리 (#90). 홈에서 노선을 고르면 바로 뜬다.
            #    알림은 기본 카드로만 보낸다
            if len(commute.rides) >= 2:
                built += await self._prewarm_rides(commute, local.date())

            # 4. 알림은 하루 한 번. FCM 이 설정돼 있을 때만 보낸 것으로 친다
            if card.notified_at is None and self._notifications and self._notifications.enabled:
                await self._notifications.notify(commute.user_id, title=CARD_TITLE, body=card.text)
                card.notified_at = now
                await self._session.commit()
        return built

    async def _prewarm_rides(self, commute: Commute, today: date) -> int:
        """계획별 카드 중 없는 것을 만든다. 한 계획의 실패가 나머지를 막지 않는다."""
        built = 0
        for ride in commute.rides:
            if await self._find_card(commute.user_id, today, ride.id) is not None:
                continue
            user = await self._users.get_user(commute.user_id)
            routes = await self._routes.list_routes(commute.id)
            if user is None or not routes:
                return built
            try:
                await self._build_and_save(
                    user, commute, routes, today, Preference(commute.preference), ride_id=ride.id
                )
                built += 1
            except AppException as exc:
                logger.warning("계획별 카드 미리 만들기 실패: %s", exc.code.name)
                await self._session.rollback()
        return built

    async def _prewarm_targets(self, user_ids: list[UUID]) -> set[UUID]:
        """미리 만들 사용자: 알림 기기가 있는 사용자 + 데모 계정."""
        targets: set[UUID] = set()
        if self._notifications is not None and user_ids:
            targets = await self._notifications.users_with_devices(user_ids)
        demo = await self._users.find_demo_user()
        if demo is not None:
            targets.add(demo.id)
        return targets

    async def push_today(self, user: User) -> int:
        """
        오늘의 카드를 내 기기에 지금 알림으로 보낸다 (시연용, #65). 카드가 없으면 만든다.

        :return: 보낸 기기 수
        :raises AppException: NOTIFICATION_NOT_CONFIGURED · COMMUTE_NOT_FOUND · ROUTES_NOT_READY
        """
        # 1. 알림을 보낼 수 있는지 먼저 본다 (안 되면 LLM 을 부르지 않는다)
        if self._notifications is None or not self._notifications.enabled:
            raise AppException(ErrorCode.NOTIFICATION_NOT_CONFIGURED)

        # 2. 오늘 카드 (캐시 또는 생성) → 발송
        card = (await self.get_today(user)).card
        sent = await self._notifications.notify(user.id, title=CARD_TITLE, body=card.text)
        card.notified_at = datetime.now(LOCAL_TZ)
        await self._session.commit()
        return sent

    async def get_demo(self, *, refresh: bool = False) -> CardResult:
        """
        둘러보기용 데모 계정의 오늘 카드. 토큰 없이 조회한다.

        :param refresh: 캐시를 무시하고 다시 만든다 (시연용). 공개 API 라 60초에 한 번만
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

        # 2. 캐시 또는 생성. refresh 는 마지막 생성 뒤 쿨다운이 지났을 때만
        today = datetime.now(LOCAL_TZ).date()
        cached = await self._find_card(user.id, today)
        cooling = (
            cached is not None and datetime.now(UTC) - cached.updated_at < DEMO_REFRESH_COOLDOWN
        )
        if cached is not None and (not refresh or cooling):
            return await self._result_from_card(cached, commute, routes, user)
        return await self._build_and_save(
            user, commute, routes, today, Preference(commute.preference)
        )

    # --- 조립 ----------------------------------------------------------------

    async def _build_and_save(
        self,
        user: User,
        commute: Commute,
        routes: list[Route],
        today: date,
        preference: Preference,
        *,
        ride_id: UUID | None = None,
    ) -> CardResult:
        # 0. 계획별 카드면 그 계획의 구간과 계획이 없는 구간(도착 후)만 본다 (#90)
        routes = _routes_for_ride(routes, ride_id)

        # 1. 오늘의 조건: 걷는 시각·일몰·날씨·미세먼지 (날씨는 실패해도 None 으로 진행)
        at = datetime.combine(today, commute.leave_time, tzinfo=LOCAL_TZ)
        weather, air = await self._weather(commute, at, user.city)
        is_clear = weather.sky == WeatherSky.CLEAR if weather.sky is not None else None
        window = sunset_window(at, lat=commute.from_lat, lng=commute.from_lng, is_clear=is_clear)

        # 2. 후보·대체 단계 (DB 미접근)
        # 오늘 카드는 퇴근 카드다 (11/3 결정). 출근 방향으로 저장된 경로를 거꾸로 본다 (#56)
        ranked = self._routes.rank(
            routes,
            preference=preference,
            at=at,
            is_clear=is_clear,
            tag_weights=user.tag_weights,
            direction=Direction.TO_HOME,
        )
        decided = decide_fallback(ranked, at=at, is_clear=is_clear)
        context = CardContext(
            commute, routes, decided, at, window.sunset_at, weather, air, window.is_open
        )

        # 3. 후보가 있으면 LLM 이 고르고, 없으면(2·4단계) 템플릿
        reply = await self._ask_llm(context, user) if decided.candidates else None
        choice = reply.parsed if reply else await self._template_choice(context)

        # 4. 행동 유형은 고른 후보의 구간이 정한다. 후보가 없는 단계는 대체 로직이 정한 값
        chosen = decided.candidates[choice.choice] if decided.candidates else None
        action = action_of(chosen) if chosen else decided.action_type

        # 5. 저장 (user_id, date) upsert. LLM 비용도 함께 남긴다
        values = {
            "user_id": user.id,
            "date": today,
            "ride_id": ride_id,
            "route_id": chosen.route.id if chosen else None,
            "action_type": str(action),
            "fallback_level": decided.level,
            "reason_tag": str(choice.reason_tag),
            "text": choice.text,
            "deviation": choice.deviation,
            "preference": str(preference),
            "sunset_at": window.sunset_at,
            "sky": str(weather.sky) if weather.sky else None,
            "air_grade": str(air.grade) if air.grade else None,
            "by_llm": reply is not None,
            "llm_input_tokens": reply.input_tokens if reply else None,
            "llm_output_tokens": reply.output_tokens if reply else None,
            "llm_ms": reply.latency_ms if reply else None,
        }
        stmt = insert(TodayCard).values(**values)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_today_cards_user_date_ride_pref",
            set_={
                k: v
                for k, v in values.items()
                if k not in ("user_id", "date", "ride_id", "preference")
            },
        )
        await self._session.execute(stmt)
        card = await self._find_card(user.id, today, ride_id, preference)
        await self._session.commit()
        assert card is not None
        journeys = await self._routes.journeys(
            commute, decided.candidates, decided.shortest, Direction.TO_HOME, user.city
        )
        return CardResult(
            card, chosen, decided.candidates, decided.shortest, journeys, list(commute.rides)
        )

    async def _ask_llm(self, context: CardContext, user: User) -> LlmReply[CardChoice] | None:
        """LLM 1회. 실패·검증 실패면 None (폴백은 호출하는 쪽)."""
        # 1. 입력 조립
        card_input = await self._card_input(context, user)

        # 2. 호출
        try:
            reply = await self._llm.complete_json_reply(
                build_prompt(card_input), CardChoice, system=SYSTEM_PROMPT
            )
        except AppException as exc:
            logger.warning("카드 LLM 실패, 템플릿으로 폴백: %s", exc.code.name)
            return None

        # 3. 검증: 후보 범위, 입력에 없는 장소·정류장 이름 (평가 스크립트와 같은 규칙)
        known = await self._routes.list_place_names(user.city)
        known |= await self._commutes.list_station_names(user.city)
        reason = reject_reason(reply.parsed, card_input, known)
        if reason is not None:
            logger.warning("카드 LLM 출력 거부, 템플릿으로 폴백: %s", reason)
            return None
        return reply

    async def _card_input(self, context: CardContext, user: User) -> CardInput:
        """LLM 입력. 경로 UUID 대신 후보마다 최근 추천 여부만 넣는다 (#59)."""
        recent = set(await self._recent_route_ids(user.id, context.at.date()))
        line_of: dict[UUID | None, str] = {ride.id: ride.line for ride in context.commute.rides}
        # 후보들의 장소·정류장을 한 번씩만 읽는다 (#101). 운영 DB 왕복이 카드 시간을 정한다
        ranked_all = context.decided.candidates
        place_by_id = {
            p.id: p
            for p in await self._routes.list_places(
                [pid for r in ranked_all for pid in r.route.spots]
            )
        }
        station_ids = [
            sid
            for r in ranked_all
            if (sid := r.alight_station_id or r.board_station_id) is not None
        ]
        station_by_id = {s.id: s for s in await self._commutes.get_stations(station_ids)}
        candidates: list[CandidateInput] = []
        for index, ranked in enumerate(ranked_all):
            places = [place_by_id[pid] for pid in ranked.route.spots if pid in place_by_id]
            station_id = ranked.alight_station_id or ranked.board_station_id
            station = station_by_id.get(station_id) if station_id else None
            candidates.append(
                CandidateInput(
                    index=index,
                    name=ranked.name,
                    action_type=str(action_of(ranked)),
                    # 문장에 "8.0분", "37.0%" 가 그대로 나오지 않게 정수로 넘긴다
                    add_minutes=round(ranked.route.add_minutes),
                    green_pct=round(ranked.route.green_pct),
                    event_score=round(ranked.event_score),
                    score=round(ranked.score),
                    tags=[str(tag) for tag in ranked.tags],
                    highlights=ranked.route.highlights,
                    season_tags=[p.season_tag for p in places if p.season_tag],
                    station=station.name if station else None,
                    skip_stops=ranked.route.skip_stops,
                    recent=ranked.route.id in recent,
                    # 계획이 둘 이상일 때만 노선명을 준다. 하나면 문장에 노선을 말할 필요가 없다
                    line=line_of.get(ranked.route.ride_id) if len(line_of) >= 2 else None,
                )
            )
        return CardInput(
            candidates=candidates,
            fallback_level=context.decided.level,
            leave_time=format_time(context.at),
            sunset_at=format_time(context.sunset_at.astimezone(LOCAL_TZ)),
            sunset_window_open=context.sunset_window_open,
            sky=str(context.weather.sky) if context.weather.sky else None,
            air_grade=str(context.air.grade) if context.air.grade else None,
            top_tag_weights=top_tags(user.tag_weights),
        )

    async def _template_choice(self, context: CardContext) -> CardChoice:
        """규칙 기반 문장. LLM 없이 점수 1위 + 템플릿."""
        decided = context.decided
        if decided.candidates:
            top = decided.candidates[0]
            route = top.route
            place = route.highlights[0] if route.highlights else top.name
            minutes = int(round(route.add_minutes))
            action = action_of(top)
            station_id = top.alight_station_id or top.board_station_id
            station = await self._commutes.get_station(station_id) if station_id else None
            station_name = station.name if station else ""
            stops = route.skip_stops
            if action == ActionType.ALIGHT_EARLY:
                lead = f"{stops}정거장 먼저 {station_name}에서 내리면 +{minutes}분,"
            elif action == ActionType.BOARD_LATE:
                lead = f"{stops}정거장 걸어가서 {station_name}에서 타면 +{minutes}분,"
            elif action == ActionType.AFTER_ARRIVAL:
                lead = f"도착 후 {minutes}분 걸으면"
            else:
                lead = f"{minutes}분 돌아가면"
            if action == ActionType.DROP_BY:
                # 왕복임을 숨기지 않는다 (#116)
                spot = top.name.removesuffix(" 잠깐 들르기")
                text = _fit(
                    f"{spot}에 잠깐 들렀다 와도 {minutes}분이면 돼요. {place}",
                    f"{spot}에 잠깐 들렀다 와도 {minutes}분이면 돼요.",
                )
            else:
                text = _fit(f"{lead} {top.name}. {place}", f"{lead} {top.name}.")
            return CardChoice(
                choice=0,
                reason_tag=_reason_from_tags(top.tags),
                text=text,
                deviation=None,
            )
        if decided.level == 2:
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
            return CardChoice(choice=0, reason_tag=tag, text=text, deviation=None)
        return CardChoice(
            choice=0,
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

    async def _find_card(
        self,
        user_id: UUID,
        today: date,
        ride_id: UUID | None = None,
        preference: Preference | None = None,
    ) -> TodayCard | None:
        """오늘 카드. preference 가 None 이면 그 계획의 아무 취향이나 (알림·미리 만들기 판정용)."""
        stmt = select(TodayCard).where(
            TodayCard.user_id == user_id,
            TodayCard.date == today,
            TodayCard.ride_id.is_(None) if ride_id is None else TodayCard.ride_id == ride_id,
        )
        if preference is not None:
            stmt = stmt.where(TodayCard.preference == Preference(preference).value)
        stmt = stmt.order_by(TodayCard.created_at).limit(1)
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
        user: User,
    ) -> CardResult:
        """캐시된 카드에 오늘의 후보 목록을 붙인다 (LLM·날씨 재호출 없음)."""
        routes = _routes_for_ride(routes, card.ride_id)
        at = datetime.combine(card.date, commute.leave_time, tzinfo=LOCAL_TZ)
        is_clear = card.sky == WeatherSky.CLEAR.value if card.sky else None
        ranked = self._routes.rank(
            routes,
            preference=Preference(card.preference),
            at=at,
            is_clear=is_clear,
            tag_weights=user.tag_weights,
            direction=Direction.TO_HOME,
        )
        decided = decide_fallback(ranked, at=at, is_clear=is_clear)
        chosen = next((r for r in ranked.eligible if r.route.id == card.route_id), None)
        journeys = await self._routes.journeys(
            commute,
            [*decided.candidates, *([chosen] if chosen else [])],
            decided.shortest,
            Direction.TO_HOME,
            user.city,
        )
        return CardResult(
            card, chosen, decided.candidates, decided.shortest, journeys, list(commute.rides)
        )


def _routes_for_ride(routes: list[Route], ride_id: UUID | None) -> list[Route]:
    """계획별 카드가 보는 경로: 그 계획의 구간 + 계획 없는 구간(도보·도착 후). 기본 카드는 전부."""
    if ride_id is None:
        return routes
    return [r for r in routes if r.ride_id is None or r.ride_id == ride_id]


def _fit(full: str, short: str) -> str:
    """템플릿 문장을 카드 길이 안에 맞춘다 (#68). 길면 장소 설명을 빼고, 그래도 길면 자른다."""
    for text in (full, short):
        if len(text) <= TEXT_MAX_CHARS:
            return text
    return short[: TEXT_MAX_CHARS - 1] + "…"


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
    notifications: NotificationService = Depends(get_notification_service),
) -> CardService:
    return CardService(
        session,
        commute_service=commute_service,
        route_service=route_service,
        user_service=user_service,
        llm=llm,
        kma=kma,
        airkorea=airkorea,
        notifications=notifications,
    )


PREWARM_INTERVAL_SECONDS = 60


async def run_prewarm_forever() -> None:
    """
    앱 안 백그라운드 루프 (#59). 1분마다 곧 퇴근하는 동선의 카드를 미리 만든다.

    Redis·메시지 큐 없이 lifespan 태스크 하나로 돈다 (서버 1대 기준).
    카드는 (사용자, 날짜) upsert 라 두 번 돌아도 한 장이다.
    """
    while True:
        try:
            async with session_factory() as session:
                service = CardService(
                    session,
                    commute_service=CommuteService(session),
                    route_service=RouteService(session, get_tmap_client()),
                    user_service=UserService(session),
                    llm=get_llm_client(),
                    kma=get_kma_client(),
                    airkorea=get_airkorea_client(),
                    notifications=NotificationService(session, get_fcm_client()),
                )
                built = await service.prewarm(datetime.now(LOCAL_TZ))
                if built:
                    logger.info("퇴근 전 카드 %d장 미리 만듦", built)
        except Exception:  # 루프는 어떤 실패에도 멈추지 않는다
            logger.exception("카드 미리 만들기 루프 실패, 다음 주기에 다시")
        await asyncio.sleep(PREWARM_INTERVAL_SECONDS)
