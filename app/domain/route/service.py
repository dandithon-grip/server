"""F-03 샛길 생성.

generate: 동선 → 걷는 구간 → 기준선 TMAP → 규칙 ①②③④로 경유지 ≤6 → TMAP ≤6 → 정적 점수 → Route 저장
rank:     저장된 Route → 오늘의 노을 창·취향·태그 가중치 적용 → 후보 조건 → 점수 상위 3개
조건값은 전부 이 파일의 상수다.
"""

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from fastapi import Depends
from geoalchemy2.shape import from_shape
from pydantic import BaseModel, ConfigDict
from shapely.geometry import LineString
from sqlalchemy import cast, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.tmap import LatLng, PedestrianRoute, TmapClient, get_tmap_client
from app.core.database import get_db
from app.core.geo import make_point
from app.domain.commute.models import Commute, Preference, Station, TransportMode
from app.domain.route.models import LegType, Place, PlaceType, Route
from app.domain.route.scoring import (
    GEOGRAPHY,
    METERS_PER_DEG_LAT,
    ScoringService,
    StaticScore,
    apply_sunset,
    meters_per_deg_lng,
    sunset_window,
)

BUFFER_M = 600
MAX_WAYPOINTS = 6
CAR_RADIUS_M = 800  # 도보 10분
EVENT_CANDIDATE_MIN = 10  # 녹지 기준 미달이어도 이벤트 점수가 이 이상이면 후보
SUNSET_PLACE_TYPES = {PlaceType.SUNSET.value, PlaceType.VIEW.value, PlaceType.BRIDGE.value}
MAX_CANDIDATES = 3

GREEN_WEIGHT = 1.0
EVENT_WEIGHT = 1.0
TAG_WEIGHT = 10.0
TAG_SUM_CLAMP = 2.0
MINUTE_PENALTY = 1.5


class PreferenceRule(BaseModel):
    """취향별 조건값 (명세 F-03 표)."""

    model_config = ConfigDict(frozen=True)

    max_add_minutes: float
    max_detour_m: float
    min_green_gain_pct: float


PREFERENCE_RULES: dict[Preference, PreferenceRule] = {
    Preference.SHORT: PreferenceRule(max_add_minutes=8, max_detour_m=600, min_green_gain_pct=5),
    Preference.FULL: PreferenceRule(max_add_minutes=15, max_detour_m=1000, min_green_gain_pct=10),
    Preference.SUNSET: PreferenceRule(max_add_minutes=15, max_detour_m=1000, min_green_gain_pct=5),
}


@dataclass(frozen=True)
class Leg:
    leg_type: LegType
    start: LatLng
    end: LatLng
    alight_station_id: UUID | None = None


@dataclass(frozen=True)
class Waypoint:
    """경유지 1개. 하천 쌍은 진입·진출 두 점이 하나다."""

    places: tuple[Place, ...]
    detour_m: float

    @property
    def pass_points(self) -> list[LatLng]:
        return [LatLng(lat=place.lat, lng=place.lng) for place in self.places]


@dataclass(frozen=True)
class RankedRoute:
    route: Route
    score: float
    event_score: float
    tags: list[PlaceType]


@dataclass(frozen=True)
class RankedRoutes:
    candidates: list[RankedRoute]  # eligible 상위 MAX_CANDIDATES
    shortest: list[RankedRoute]  # 구간별 기준선
    eligible: list[RankedRoute]  # 후보 조건을 통과한 전부, 점수순 (F-15 가 구간별로 본다)


def distance_m(a: LatLng, b: LatLng) -> float:
    """두 점 사이 직선 거리 (m). 도시 규모 평면 근사."""
    dx = (b.lng - a.lng) * meters_per_deg_lng((a.lat + b.lat) / 2)
    dy = (b.lat - a.lat) * METERS_PER_DEG_LAT
    return math.hypot(dx, dy)


def detour_m(start: LatLng, via: list[LatLng], end: LatLng) -> float:
    """직선 우회량 dist(A,W1)+dist(W1,W2)+...+dist(Wn,B) − dist(A,B)."""
    path = [start, *via, end]
    along = sum(distance_m(a, b) for a, b in zip(path, path[1:], strict=False))
    return along - distance_m(start, end)


def order_pair(start: LatLng, places: list[Place]) -> tuple[Place, ...]:
    """하천 쌍은 출발점에 가까운 쪽을 진입점으로 둔다."""
    return tuple(sorted(places, key=lambda p: distance_m(start, LatLng(lat=p.lat, lng=p.lng))))


def shortlist(
    start: LatLng, end: LatLng, places: list[Place], preference: Preference
) -> list[Waypoint]:
    """
    규칙 ②③④. ①(600m 버퍼)은 DB 가 했다.

    :param places: 기준선 600m 안의 Place
    :return: 우회량 적은 순 경유지 ≤ MAX_WAYPOINTS
    """
    rule = PREFERENCE_RULES[preference]

    # 1. 하천 쌍 묶기. pair_id 가 같은 둘은 경유지 1개
    groups: dict[str, list[Place]] = defaultdict(list)
    singles: list[Place] = []
    for place in places:
        if place.pair_id is not None:
            groups[place.pair_id].append(place)
        else:
            singles.append(place)
    units: list[tuple[Place, ...]] = [(place,) for place in singles]
    for members in groups.values():
        units.append(order_pair(start, members))

    # 2. ③ sunset 이면 노을·전망·다리 지점만
    if preference == Preference.SUNSET:
        units = [u for u in units if any(set(p.types) & SUNSET_PLACE_TYPES for p in u)]

    # 3. ② 직선 우회량 ≤ 취향별 상한
    waypoints = [
        Waypoint(places=unit, detour_m=d)
        for unit in units
        if (d := detour_m(start, [LatLng(lat=p.lat, lng=p.lng) for p in unit], end))
        <= rule.max_detour_m
    ]

    # 4. ④ 6개 초과면 우회량 적은 순
    waypoints.sort(key=lambda w: w.detour_m)
    return waypoints[:MAX_WAYPOINTS]


class RouteService:
    def __init__(self, session: AsyncSession, tmap: TmapClient) -> None:
        self._session = session
        self._tmap = tmap
        self._scoring = ScoringService(session)

    # --- generate ----------------------------------------------------------

    async def generate(self, commute: Commute, city: str) -> list[Route]:
        """
        동선의 걷는 구간마다 기준선과 경유지 후보 경로를 만들어 저장한다. 기존 경로는 지운다.

        :param commute: 저장된 동선 (좌표·교통수단·정류장·취향)
        :param city: 도시 코드
        :return: 저장된 Route (기준선 포함)
        :raises AppException: EXTERNAL_API_FAILED - TMAP 실패
        """
        # 1. 걷는 구간
        legs = await self._legs(commute)

        # 2. 기존 경로 삭제
        await self._session.execute(delete(Route).where(Route.commute_id == commute.id))

        routes: list[Route] = []
        for leg in legs:
            if leg.leg_type == LegType.AFTER_ARRIVAL:
                routes.extend(await self._after_arrival_routes(commute, leg, city))
                continue

            # 3. 기준선 TMAP 1회
            baseline = await self._tmap.pedestrian_route(leg.start, leg.end)
            routes.append(await self._build_route(commute, leg, baseline, [], baseline, city))

            # 4. 규칙 ① 600m 버퍼 → ②③④
            nearby = await self._places_near(baseline.polyline, city)
            waypoints = shortlist(leg.start, leg.end, nearby, commute.preference)

            # 5. 경유지마다 TMAP → 정적 점수
            for waypoint in waypoints:
                detoured = await self._tmap.pedestrian_route(
                    leg.start, leg.end, pass_points=waypoint.pass_points
                )
                routes.append(
                    await self._build_route(
                        commute, leg, detoured, list(waypoint.places), baseline, city
                    )
                )

        # 6. 저장
        self._session.add_all(routes)
        await self._session.commit()
        return routes

    async def _legs(self, commute: Commute) -> list[Leg]:
        """걷는 구간. 도착지 반경 구간(F-15 4단계용)은 모든 교통수단에 붙인다."""
        start = LatLng(lat=commute.from_lat, lng=commute.from_lng)
        end = LatLng(lat=commute.to_lat, lng=commute.to_lng)
        after_arrival = Leg(LegType.AFTER_ARRIVAL, end, end)
        if commute.transport_mode == TransportMode.WALK:
            return [Leg(LegType.WALK_ALL, start, end), after_arrival]
        if commute.transport_mode == TransportMode.CAR:
            return [after_arrival]

        # transit: 앞 도보 · 뒤 도보 · 한 정거장 먼저
        stations = await self._stations(commute.station_ids)
        first, last = stations[0], stations[-1]
        legs = [
            Leg(LegType.FRONT_WALK, start, LatLng(lat=first.lat, lng=first.lng)),
            Leg(LegType.BACK_WALK, LatLng(lat=last.lat, lng=last.lng), end),
        ]
        previous = await self._previous_station(first, last)
        if previous is not None:
            legs.append(
                Leg(
                    LegType.ALIGHT_EARLY,
                    LatLng(lat=previous.lat, lng=previous.lng),
                    end,
                    alight_station_id=previous.id,
                )
            )
        legs.append(after_arrival)
        return legs

    async def _stations(self, station_ids: list[UUID]) -> list[Station]:
        stmt = select(Station).where(Station.id.in_(station_ids))
        by_id = {s.id: s for s in (await self._session.execute(stmt)).scalars()}
        return [by_id[station_id] for station_id in station_ids if station_id in by_id]

    async def _previous_station(self, first: Station, last: Station) -> Station | None:
        """마지막 정류장 바로 앞 정류장 (진행 방향 기준). 같은 역이거나 없으면 None."""
        if first.id == last.id or first.line != last.line:
            return None
        direction = 1 if last.order_no > first.order_no else -1
        stmt = select(Station).where(
            Station.city == last.city,
            Station.line == last.line,
            Station.order_no == last.order_no - direction,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def _places_near(self, polyline: list[LatLng], city: str) -> list[Place]:
        """규칙 ①: 기준선 600m 안의 Place."""
        line = from_shape(LineString([(p.lng, p.lat) for p in polyline]), srid=4326)
        stmt = select(Place).where(
            Place.city == city,
            func.ST_DWithin(cast(Place.location, GEOGRAPHY), cast(line, GEOGRAPHY), BUFFER_M),
        )
        return list((await self._session.execute(stmt)).scalars())

    async def _after_arrival_routes(self, commute: Commute, leg: Leg, city: str) -> list[Route]:
        """자동차: 도착지 반경 10분 안의 Place 로 가는 경로. 기준선 없음(추가 시간 = 소요 시간)."""
        stmt = select(Place).where(
            Place.city == city,
            func.ST_DWithin(
                cast(Place.location, GEOGRAPHY),
                cast(make_point(leg.end.lat, leg.end.lng), GEOGRAPHY),
                CAR_RADIUS_M,
            ),
        )
        places = list((await self._session.execute(stmt)).scalars())
        units = shortlist(leg.start, leg.end, places, commute.preference)
        routes: list[Route] = []
        for waypoint in units:
            target = waypoint.places[-1]
            walked = await self._tmap.pedestrian_route(
                leg.end, LatLng(lat=target.lat, lng=target.lng)
            )
            routes.append(
                await self._build_route(commute, leg, walked, list(waypoint.places), None, city)
            )
        return routes

    async def _build_route(
        self,
        commute: Commute,
        leg: Leg,
        walked: PedestrianRoute,
        places: list[Place],
        baseline: PedestrianRoute | None,
        city: str,
    ) -> Route:
        static = await self._scoring.score_static(walked.polyline, city=city)
        base_minutes = baseline.minutes if baseline is not None else 0.0
        is_baseline = baseline is walked
        return Route(
            commute_id=commute.id,
            leg_type=leg.leg_type,
            is_baseline=is_baseline,
            name=_route_name(leg, places),
            minutes=round(walked.minutes, 1),
            add_minutes=round(max(walked.minutes - base_minutes, 0.0), 1),
            polyline=from_shape(LineString([(p.lng, p.lat) for p in walked.polyline]), srid=4326),
            green_pct=static.green_pct,
            water_score=static.water_score,
            landmark_score=static.landmark_score,
            sunset_ratio=static.sunset_ratio,
            tags=[tag.value for tag in static.tags],
            spots=[place.id for place in places],
            highlights=[place.description for place in places],
            alight_station_id=leg.alight_station_id,
        )

    # --- rank ---------------------------------------------------------------

    async def list_routes(self, commute_id: UUID) -> list[Route]:
        stmt = select(Route).where(Route.commute_id == commute_id).order_by(Route.created_at)
        return list((await self._session.execute(stmt)).scalars())

    def rank(
        self,
        routes: list[Route],
        *,
        preference: Preference,
        at: datetime,
        is_clear: bool | None,
        tag_weights: dict[str, float] | None = None,
    ) -> RankedRoutes:
        """
        저장된 경로에 오늘의 조건을 적용해 후보 ≤3개를 고른다. DB 를 읽지 않는다.

        :param routes: 한 동선의 Route 전부 (기준선 포함)
        :param preference: 취향. 기본 취향 또는 취향 칩
        :param at: 걷는 시각 (tz-aware). 노을 창 판정
        :param is_clear: 맑음 여부. None 이면 노을 가산 없음
        :param tag_weights: F-16 태그 가중치. 없으면 0
        """
        rule = PREFERENCE_RULES[preference]
        weights = tag_weights or {}

        # 1. 구간별 기준선 녹지 %
        base_green = {r.leg_type: r.green_pct for r in routes if r.is_baseline}

        # 2. 노을 창 (동선의 첫 좌표 기준)
        window_open = False
        if routes:
            lat, lng = routes[0].points[0]
            window_open = sunset_window(at, lat=lat, lng=lng, is_clear=is_clear).is_open

        ranked: list[RankedRoute] = []
        shortest: list[RankedRoute] = []
        for route in routes:
            score_today = apply_sunset(_static_of(route), window_open=window_open)
            if route.is_baseline:
                shortest.append(RankedRoute(route, 0.0, score_today.event_score, score_today.tags))
                continue

            # 3. 후보 조건: 추가 시간 상한 그리고 (녹지 기준 또는 이벤트 ≥ 10)
            green_gain = route.green_pct - base_green.get(route.leg_type, 0.0)
            if route.add_minutes > rule.max_add_minutes:
                continue
            if (
                green_gain < rule.min_green_gain_pct
                and score_today.event_score < EVENT_CANDIDATE_MIN
            ):
                continue

            # 4. 점수 = 녹지 + 이벤트 + clamp(Σ가중치, ±2) × 10 − 추가 분 × 1.5
            weight_sum = sum(weights.get(tag.value, 0.0) for tag in score_today.tags)
            clamped = max(-TAG_SUM_CLAMP, min(TAG_SUM_CLAMP, weight_sum))
            score = (
                route.green_pct * GREEN_WEIGHT
                + score_today.event_score * EVENT_WEIGHT
                + clamped * TAG_WEIGHT
                - route.add_minutes * MINUTE_PENALTY
            )
            ranked.append(
                RankedRoute(route, round(score, 1), score_today.event_score, score_today.tags)
            )

        # 5. 상위 3개
        ranked.sort(key=lambda r: r.score, reverse=True)
        return RankedRoutes(candidates=ranked[:MAX_CANDIDATES], shortest=shortest, eligible=ranked)


def _static_of(route: Route) -> StaticScore:
    return StaticScore(
        green_pct=route.green_pct,
        water_score=route.water_score,
        landmark_score=route.landmark_score,
        sunset_ratio=route.sunset_ratio,
        tags=[PlaceType(value) for value in route.tags],
    )


def _route_name(leg: Leg, places: list[Place]) -> str:
    if not places:
        return "최단 경로"
    base = places[0].name.replace(" 진입", "").replace(" 진출", "")
    suffixes = {LegType.ALIGHT_EARLY: " (한 정거장 먼저)", LegType.AFTER_ARRIVAL: " (도착 후)"}
    return f"{base} 샛길{suffixes.get(leg.leg_type, '')}"


def get_route_service(
    session: AsyncSession = Depends(get_db), tmap: TmapClient = Depends(get_tmap_client)
) -> RouteService:
    return RouteService(session, tmap)
