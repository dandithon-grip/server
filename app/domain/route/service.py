"""F-03 샛길 생성.

generate: 동선 → 걷는 구간(1~3 정거장 먼저 내리기·늦게 타기 포함) → 기준선 TMAP → 규칙 ①②③④로
          경유지 ≤6 → TMAP ≤6 → 왕복 제외 → 정적 점수 → Route 저장. TMAP 은 동시에 부른다
rank:     저장된 Route → 오늘의 노을 창·취향·태그 가중치 적용 → 후보 조건 → 점수 상위 3개
조건값은 전부 이 파일의 상수다.
"""

import asyncio
import math
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from uuid import UUID

from fastapi import Depends
from geoalchemy2.shape import from_shape
from pydantic import BaseModel, ConfigDict
from shapely.geometry import LineString
from sqlalchemy import cast, delete, func, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.tmap import LatLng, PedestrianRoute, TmapClient, get_tmap_client
from app.core.database import get_db
from app.core.geo import make_point
from app.domain.commute.models import Commute, CommuteRide, Preference, Station, TransportMode
from app.domain.route.journey import Ride, Segment, compose_journey, orient_rides
from app.domain.route.models import (
    MIRROR_LEG,
    Direction,
    GreenArea,
    GreenKind,
    LegType,
    Place,
    PlaceType,
    Route,
)
from app.domain.route.scoring import (
    GEOGRAPHY,
    METERS_PER_DEG_LAT,
    ScoringService,
    StaticScore,
    apply_sunset,
    bbox_near,
    meters_per_deg_lng,
    retrace_m,
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
# 지나온 길을 이만큼 넘게 다시 밟으면 샛길이 아니라 왕복이라 저장하지 않는다 (#49)
RETRACE_MAX_M = 100
# 안에 있는 자연 지점을 가장자리로 옮기는 녹지 종류. 하천·가로수열은 선이라 해당 없음 (#82)
AREA_GREEN_KINDS = [GreenKind.PARK.value, GreenKind.FOREST.value, GreenKind.GRASS.value]
# 정거장을 건너뛰면 그만큼 덜 탄다. 노선별 실측 없이 정거장당 평균으로 뺀다 (#51)
RIDE_MINUTES_PER_STOP = 2.0
# 동시에 부르는 TMAP 요청 수. 신규 동선 10초 안 (명세 F-03 성능)
TMAP_CONCURRENCY = 4


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


# 건너뛰기 구간의 추가 시간·녹지 증가는 원래 계획의 이 구간과 비교한다 (#51)
REFERENCE_LEG = {LegType.ALIGHT_EARLY: LegType.BACK_WALK, LegType.BOARD_LATE: LegType.FRONT_WALK}
MAX_ADD_MINUTES = max(rule.max_add_minutes for rule in PREFERENCE_RULES.values())


@dataclass(frozen=True)
class Leg:
    leg_type: LegType
    start: LatLng
    end: LatLng
    alight_station_id: UUID | None = None
    board_station_id: UUID | None = None
    skip_stops: int = 0  # 먼저 내리기·늦게 타기에서 건너뛴 정거장 수
    station_name: str | None = None
    direction: Direction = Direction.TO_WORK  # 경로선을 만드는 방향
    ride_id: UUID | None = None  # 어느 탑승 계획의 구간인지 (#88). 도보·자동차·도착 후는 None

    @property
    def reference_key(self) -> tuple[UUID | None, LegType]:
        """추가 시간·녹지 비교 기준선의 키. 같은 계획의 구간끼리만 비교한다 (#88)."""
        return (self.ride_id, self.reference)

    @property
    def reference(self) -> LegType:
        """추가 시간·녹지 증가를 잴 기준 구간. 건너뛰기면 원래 계획의 도보 구간."""
        return REFERENCE_LEG.get(self.leg_type, self.leg_type)


@dataclass(frozen=True)
class Waypoint:
    """경유지 1개. 하천 쌍은 진입·진출 두 점이 하나다.

    points 는 TMAP 에 넘길 좌표. 공원 안 지점은 가장자리로 옮겨져 Place 좌표와 다를 수 있다 (#82).
    """

    places: tuple[Place, ...]
    detour_m: float
    points: tuple[LatLng, ...]

    @property
    def pass_points(self) -> list[LatLng]:
        return list(self.points)


@dataclass(frozen=True)
class RankedRoute:
    """오늘 조건을 적용한 경로. 구간·이름·정류장·좌표는 걷는 방향 기준이다 (#56)."""

    route: Route
    score: float
    event_score: float
    tags: list[PlaceType]
    leg_type: LegType
    name: str
    points: list[tuple[float, float]]
    alight_station_id: UUID | None
    board_station_id: UUID | None
    sunset_ratio: float


def oriented(
    route: Route, direction: Direction, *, score: float, event_score: float, tags: list[PlaceType]
) -> RankedRoute:
    """저장된 경로를 걷는 방향으로 본다. 반대 방향이면 구간·정류장·좌표를 뒤집는다."""
    stored = LegType(route.leg_type)
    same = direction == route.stored_direction
    points = route.points if same else list(reversed(route.points))
    return RankedRoute(
        route=route,
        score=score,
        event_score=event_score,
        tags=tags,
        leg_type=stored if same else MIRROR_LEG[stored],
        name=route.name_for(direction),
        points=points,
        alight_station_id=route.alight_station_id if same else route.board_station_id,
        board_station_id=route.board_station_id if same else route.alight_station_id,
        sunset_ratio=route.sunset_ratio_for(direction),
    )


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
    start: LatLng,
    end: LatLng,
    places: list[Place],
    preference: Preference,
    entry_points: Mapping[UUID, LatLng] | None = None,
) -> list[Waypoint]:
    """
    규칙 ②③④. ①(600m 버퍼)은 DB 가 했다.

    :param places: 기준선 600m 안의 Place
    :param entry_points: Place id → 실제 경유 좌표 (공원 안 지점의 가장자리, #82). 없으면 Place 좌표
    :return: 우회량 적은 순 경유지 ≤ MAX_WAYPOINTS
    """
    entries = entry_points or {}

    def point_of(place: Place) -> LatLng:
        return entries.get(place.id) or LatLng(lat=place.lat, lng=place.lng)

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

    # 3. ② 직선 우회량 ≤ 취향별 상한. 우회량도 실제 경유 좌표로 잰다
    waypoints = [
        Waypoint(places=unit, detour_m=d, points=points)
        for unit in units
        if (d := detour_m(start, list(points := tuple(point_of(p) for p in unit)), end))
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

        먼저 내리기·늦게 타기 구간은 기준선이 아니라 후보다. 그냥 걷는 길도 녹지가 늘면 샛길이다.
        추가 시간은 원래 계획(앞·뒤 도보 최단)과 비교하고, 덜 탄 정거장만큼 뺀다.

        :param commute: 저장된 동선 (좌표·교통수단·정류장·취향)
        :param city: 도시 코드
        :return: 저장된 Route (기준선 포함)
        :raises AppException: EXTERNAL_API_FAILED - TMAP 실패
        """
        # 1. 걷는 구간
        legs = await self._legs(commute)
        walking = [leg for leg in legs if leg.leg_type != LegType.AFTER_ARRIVAL]

        # 2. 기존 경로 삭제
        await self._session.execute(delete(Route).where(Route.commute_id == commute.id))

        # 3. 구간별 최단 경로 TMAP (동시). 계획끼리 같은 구간(같은 승차 정류장)은 한 번만 부른다
        shortest = await self._walk_all([(leg.start, leg.end, None) for leg in walking])
        reference_minutes = {
            leg.reference_key: walked.minutes
            for leg, walked in zip(walking, shortest, strict=True)
            if leg.skip_stops == 0
        }

        # 4. 최단 경로 저장 + 경유지 추리기. 그냥 걸어도 상한을 넘는 건너뛰기 구간은 버린다
        routes: list[Route] = []
        plans: list[tuple[Leg, Waypoint]] = []
        for leg, walked in zip(walking, shortest, strict=True):
            extra = _extra_minutes(leg, walked, reference_minutes)
            if leg.skip_stops and extra > MAX_ADD_MINUTES:
                continue
            routes.append(
                await self._build_route(
                    commute, leg, walked, [], extra, is_baseline=leg.skip_stops == 0, city=city
                )
            )
            nearby = await self._places_near(walked.polyline, city)
            entries = await self.entry_points(nearby, walked.polyline, city)
            plans += [
                (leg, wp)
                for wp in shortlist(leg.start, leg.end, nearby, commute.preference, entries)
            ]

        # 5. 경유지마다 TMAP (동시) → 왕복 제외 → 정적 점수
        detoured = await self._walk_all([(leg.start, leg.end, wp.pass_points) for leg, wp in plans])
        for (leg, waypoint), walked in zip(plans, detoured, strict=True):
            if retrace_m(walked.polyline) > RETRACE_MAX_M:
                continue
            extra = _extra_minutes(leg, walked, reference_minutes)
            routes.append(
                await self._build_route(
                    commute, leg, walked, list(waypoint.places), extra, is_baseline=False, city=city
                )
            )

        # 6. 도착 후 구간
        for leg in legs:
            if leg.leg_type == LegType.AFTER_ARRIVAL:
                routes += await self._after_arrival_routes(commute, leg, city)

        # 7. 저장
        self._session.add_all(routes)
        await self._session.commit()
        return routes

    async def _walk_all(
        self, requests: list[tuple[LatLng, LatLng, list[LatLng] | None]]
    ) -> list[PedestrianRoute]:
        """TMAP 보행 경로를 동시에 부른다. 동시 요청 수는 TMAP_CONCURRENCY 로 묶는다.

        같은 (출발, 도착, 경유) 요청은 한 번만 부르고 결과를 나눠 쓴다 (#88: 계획이 공유하는 구간).
        """
        semaphore = asyncio.Semaphore(TMAP_CONCURRENCY)

        async def walk(start: LatLng, end: LatLng, via: list[LatLng] | None) -> PedestrianRoute:
            async with semaphore:
                return await self._tmap.pedestrian_route(start, end, pass_points=via)

        def key(request: tuple[LatLng, LatLng, list[LatLng] | None]) -> tuple[object, ...]:
            start, end, via = request
            return (
                start.lat,
                start.lng,
                end.lat,
                end.lng,
                tuple((p.lat, p.lng) for p in via or []),
            )

        unique: dict[tuple[object, ...], tuple[LatLng, LatLng, list[LatLng] | None]] = {}
        for request in requests:
            unique.setdefault(key(request), request)
        results = await asyncio.gather(*(walk(*request) for request in unique.values()))
        by_key = dict(zip(unique.keys(), results, strict=True))
        return [by_key[key(request)] for request in requests]

    async def _legs(self, commute: Commute) -> list[Leg]:
        """걷는 구간 (출근 방향). 도착지 반경 구간(F-15 3단계)은 방향마다 모든 교통수단에 붙인다."""
        start = LatLng(lat=commute.from_lat, lng=commute.from_lng)
        end = LatLng(lat=commute.to_lat, lng=commute.to_lng)
        # 도착 후는 방향마다 다른 곳이다: 출근이면 회사 주변, 퇴근이면 집 주변 (#56)
        after_arrival = [
            Leg(LegType.AFTER_ARRIVAL, end, end, direction=Direction.TO_WORK),
            Leg(LegType.AFTER_ARRIVAL, start, start, direction=Direction.TO_HOME),
        ]
        if commute.transport_mode == TransportMode.WALK:
            return [Leg(LegType.WALK_ALL, start, end), *after_arrival]
        if commute.transport_mode == TransportMode.CAR:
            return after_arrival

        # transit: 탑승 계획마다 앞 도보(출발→첫 탑승) · 뒤 도보(마지막 하차→도착)
        # · 1~3 정거장 먼저 내려 걷기 · 1~3 정거장 걸어가서 타기 (#88).
        # station_ids 는 [탑승, 하차, 탑승, 하차, ...] 순서. 건너뛸 정류장은 동선 저장 때 정해 뒀다
        plans = ride_plans(commute)
        station = await self._stations(
            [
                sid
                for plan in plans
                for sid in (
                    *plan.station_ids,
                    *plan.alight_early_station_ids,
                    *plan.board_late_station_ids,
                )
            ]
        )
        legs: list[Leg] = []
        for plan in plans:
            if not plan.station_ids:
                continue
            first, last = station[plan.station_ids[0]], station[plan.station_ids[-1]]
            legs.append(Leg(LegType.FRONT_WALK, start, _at(first), ride_id=plan.id))
            legs.append(Leg(LegType.BACK_WALK, _at(last), end, ride_id=plan.id))
            for skip, station_id in enumerate(plan.alight_early_station_ids, start=1):
                if station_id in station:
                    legs.append(
                        Leg(
                            LegType.ALIGHT_EARLY,
                            _at(station[station_id]),
                            end,
                            alight_station_id=station_id,
                            skip_stops=skip,
                            station_name=station[station_id].name,
                            ride_id=plan.id,
                        )
                    )
            for skip, station_id in enumerate(plan.board_late_station_ids, start=1):
                if station_id in station:
                    legs.append(
                        Leg(
                            LegType.BOARD_LATE,
                            start,
                            _at(station[station_id]),
                            board_station_id=station_id,
                            skip_stops=skip,
                            station_name=station[station_id].name,
                            ride_id=plan.id,
                        )
                    )
        legs += after_arrival
        return legs

    async def _stations(self, station_ids: list[UUID]) -> dict[UUID, Station]:
        stmt = select(Station).where(Station.id.in_(station_ids))
        return {s.id: s for s in (await self._session.execute(stmt)).scalars()}

    async def _places_near(self, polyline: list[LatLng], city: str) -> list[Place]:
        """규칙 ①: 기준선 600m 안의 Place."""
        line = from_shape(LineString([(p.lng, p.lat) for p in polyline]), srid=4326)
        stmt = select(Place).where(
            Place.city == city,
            bbox_near(Place.location, line, BUFFER_M, polyline[0].lat),
            func.ST_DWithin(cast(Place.location, GEOGRAPHY), cast(line, GEOGRAPHY), BUFFER_M),
        )
        return list((await self._session.execute(stmt)).scalars())

    async def entry_points(
        self, places: list[Place], polyline: list[LatLng], city: str
    ) -> dict[UUID, LatLng]:
        """
        공원·숲·잔디 폴리곤 안 Place 의 경유 좌표: 기준선 쪽 경계점 (#82).

        공원 한가운데를 경유지로 주면 TMAP 이 들어갔다 같은 길로 나와 왕복 필터(#49)에 탈락한다.
        가장자리를 지나가게 하면 왕복이 없고 녹지 % 는 30m 버퍼로 그대로 잡힌다.
        폴리곤 밖 Place 는 결과에 없다 (Place 좌표 그대로 쓴다).

        :param places: 기준선 600m 안의 Place
        :param polyline: 기준선 (TMAP 최단 경로)
        :param city: 도시 코드
        :return: Place id → 경계점
        """
        if not places:
            return {}
        line = from_shape(LineString([(p.lng, p.lat) for p in polyline]), srid=4326)
        # 기준선에서 그 지점과 가장 가까운 자리(공원 옆을 지나는 곳) → 거기서 가장 가까운 경계점.
        # 경계를 기준선과 바로 비교하면 변이 평행할 때 모서리가 나와 지점에서 멀어진다
        foot = func.ST_ClosestPoint(line, Place.location)
        boundary_point = func.ST_ClosestPoint(func.ST_Boundary(GreenArea.geom), foot)
        stmt = (
            select(Place.id, func.ST_Y(boundary_point), func.ST_X(boundary_point))
            .select_from(Place)
            .join(GreenArea, func.ST_Covers(GreenArea.geom, Place.location))
            .where(
                Place.id.in_([p.id for p in places]),
                GreenArea.city == city,
                GreenArea.kind.in_(AREA_GREEN_KINDS),
                func.GeometryType(GreenArea.geom).in_(["POLYGON", "MULTIPOLYGON"]),
            )
            # 겹치는 폴리곤이 있으면 큰 쪽(공원 전체) 기준
            .order_by(Place.id, func.ST_Area(GreenArea.geom).desc())
            .ext(distinct_on(Place.id))
        )
        rows = (await self._session.execute(stmt)).all()
        return {place_id: LatLng(lat=float(lat), lng=float(lng)) for place_id, lat, lng in rows}

    async def _after_arrival_routes(self, commute: Commute, leg: Leg, city: str) -> list[Route]:
        """도착지 반경 10분 안의 Place 로 가는 경로. 기준선 없음(추가 시간 = 소요 시간)."""
        end_point = make_point(leg.end.lat, leg.end.lng)
        stmt = select(Place).where(
            Place.city == city,
            bbox_near(Place.location, end_point, CAR_RADIUS_M, leg.end.lat),
            func.ST_DWithin(
                cast(Place.location, GEOGRAPHY), cast(end_point, GEOGRAPHY), CAR_RADIUS_M
            ),
        )
        places = list((await self._session.execute(stmt)).scalars())
        units = shortlist(leg.start, leg.end, places, commute.preference)
        targets = [waypoint.places[-1] for waypoint in units]
        walked = await self._walk_all(
            [(leg.end, LatLng(lat=target.lat, lng=target.lng), None) for target in targets]
        )
        return [
            await self._build_route(
                commute, leg, route, list(unit.places), route.minutes, is_baseline=False, city=city
            )
            for unit, route in zip(units, walked, strict=True)
        ]

    async def _build_route(
        self,
        commute: Commute,
        leg: Leg,
        walked: PedestrianRoute,
        places: list[Place],
        add_minutes: float,
        *,
        is_baseline: bool,
        city: str,
    ) -> Route:
        static = await self._scoring.score_static(walked.polyline, city=city)
        return Route(
            commute_id=commute.id,
            leg_type=leg.leg_type,
            is_baseline=is_baseline,
            name=_route_name(leg, places),
            minutes=round(walked.minutes, 1),
            add_minutes=round(max(add_minutes, 0.0), 1),
            polyline=from_shape(LineString([(p.lng, p.lat) for p in walked.polyline]), srid=4326),
            green_pct=static.green_pct,
            water_score=static.water_score,
            landmark_score=static.landmark_score,
            sunset_ratio=static.sunset_ratio,
            tags=[tag.value for tag in static.tags],
            spots=[place.id for place in places],
            highlights=[place.description for place in places],
            alight_station_id=leg.alight_station_id,
            board_station_id=leg.board_station_id,
            ride_id=leg.ride_id,
            skip_stops=leg.skip_stops,
            direction=leg.direction,
            sunset_ratio_reverse=static.sunset_ratio_reverse,
            name_reverse=_route_name(replace(leg, leg_type=MIRROR_LEG[leg.leg_type]), places),
        )

    async def get_place(self, place_id: UUID) -> Place | None:
        """
        큐레이션 자연 지점 하나를 조회한다. 없으면 None.

        Place 는 이 도메인의 엔티티다. 다른 도메인(기록 F-11)이 place_id 가 실재하는지
        확인할 때 이 메서드를 쓴다.
        """
        stmt = select(Place).where(Place.id == place_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def find_user_route(self, user_id: UUID, route_id: UUID) -> Route | None:
        """사용자의 동선에 속한 경로. 남의 경로면 None (피드백은 내 경로에만)."""
        stmt = (
            select(Route)
            .join(Commute, Commute.id == Route.commute_id)
            .where(Route.id == route_id, Commute.user_id == user_id)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_places(self, place_ids: list[UUID]) -> list[Place]:
        """id 목록의 Place. 카드가 경유지의 계절 태그·이름을 쓴다."""
        if not place_ids:
            return []
        stmt = select(Place).where(Place.id.in_(place_ids))
        return list((await self._session.execute(stmt)).scalars())

    async def list_place_names(self, city: str) -> set[str]:
        """도시의 모든 Place 이름. 카드 문장에 입력에 없는 장소가 섞였는지 검사한다."""
        stmt = select(Place.name).where(Place.city == city)
        return set((await self._session.execute(stmt)).scalars())

    async def find_nearest_place(
        self, lat: float, lng: float, *, city: str, radius_m: float
    ) -> Place | None:
        """
        현재 위치 반경 안에서 가장 가까운 큐레이션 지점을 찾는다. 없으면 None.

        :param lat: 위도
        :param lng: 경도
        :param city: 도시 코드
        :param radius_m: 반경 (m)
        """
        location = cast(Place.location, GEOGRAPHY)
        here = cast(make_point(lat, lng), GEOGRAPHY)
        stmt = (
            select(Place)
            .where(Place.city == city, func.ST_DWithin(location, here, radius_m))
            .order_by(func.ST_Distance(location, here))
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

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
        direction: Direction = Direction.TO_WORK,
    ) -> RankedRoutes:
        """
        저장된 경로에 오늘의 조건을 적용해 후보 ≤3개를 고른다. DB 를 읽지 않는다.

        경로는 출근 방향으로 저장돼 있다. direction 이 퇴근이면 같은 길을 거꾸로 걷는 것으로 보고
        구간·이름·정류장·좌표·노을 비율을 뒤집는다. 도착 후 구간은 그 방향 것만 쓴다 (#56).

        :param routes: 한 동선의 Route 전부 (기준선 포함)
        :param preference: 취향. 기본 취향 또는 취향 칩
        :param at: 걷는 시각 (tz-aware). 노을 창 판정
        :param is_clear: 맑음 여부. None 이면 노을 가산 없음
        :param tag_weights: F-16 태그 가중치. 없으면 0
        :param direction: 걷는 방향. 오늘 카드는 퇴근
        """
        rule = PREFERENCE_RULES[preference]
        weights = tag_weights or {}
        routes = [
            r
            for r in routes
            if LegType(r.leg_type) != LegType.AFTER_ARRIVAL or r.stored_direction == direction
        ]

        # 1. (탑승 계획, 구간)별 기준선 녹지 % (#88)
        base_green = {
            (r.ride_id, LegType(r.leg_type)): r.green_pct for r in routes if r.is_baseline
        }

        # 2. 노을 창 (동선의 첫 좌표 기준)
        window_open = False
        if routes:
            lat, lng = routes[0].points[0]
            window_open = sunset_window(at, lat=lat, lng=lng, is_clear=is_clear).is_open

        ranked: list[RankedRoute] = []
        shortest: list[RankedRoute] = []
        for route in routes:
            score_today = apply_sunset(_static_of(route, direction), window_open=window_open)
            if route.is_baseline:
                shortest.append(
                    oriented(
                        route,
                        direction,
                        score=0.0,
                        event_score=score_today.event_score,
                        tags=score_today.tags,
                    )
                )
                continue

            # 3. 후보 조건: 추가 시간 상한 그리고 (녹지 기준 또는 이벤트 ≥ 10)
            # 건너뛰기 구간은 원래 계획(앞·뒤 도보 최단)의 녹지와 비교한다.
            # 저장된 방향끼리 비교하므로 걷는 방향과 무관하다
            reference = REFERENCE_LEG.get(LegType(route.leg_type), LegType(route.leg_type))
            green_gain = route.green_pct - base_green.get((route.ride_id, reference), 0.0)
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
                oriented(
                    route,
                    direction,
                    score=round(score, 1),
                    event_score=score_today.event_score,
                    tags=score_today.tags,
                )
            )

        # 5. 상위 3개
        ranked.sort(key=lambda r: r.score, reverse=True)
        return RankedRoutes(candidates=ranked[:MAX_CANDIDATES], shortest=shortest, eligible=ranked)

    # --- journey (#77) ------------------------------------------------------

    async def journeys(
        self,
        commute: Commute,
        routes: list[RankedRoute],
        shortest: list[RankedRoute],
        direction: Direction,
        city: str,
    ) -> dict[UUID, list[Segment]]:
        """
        경로마다 그것을 골랐을 때의 출발 → 도착 전체 여정. 정류장 조회는 한 번이다.

        탑승 계획이 여러 개면 경로가 속한 계획의 탑승·기준선으로 잇는다 (#88).
        계획이 없는 경로(도보 통근·도착 후)는 기본 계획(0) 으로 잇는다.

        :param commute: 동선 (교통수단·탑승 계획·좌표)
        :param routes: 걷는 방향으로 본 후보
        :param shortest: 걷는 방향으로 본 구간별 기준선
        :param direction: 걷는 방향
        :param city: 도시 코드. 정류장은 도시별이다
        :return: Route id → 조각 목록 (후보와 기준선 모두)
        """
        # 1. 계획별 탑승 목록 (출근 방향) → 걷는 방향
        rides_of: dict[UUID | None, list[Ride]] = {}
        plans = ride_plans(commute) if commute.transport_mode == TransportMode.TRANSIT else []
        if plans:
            by_plan = await self._rides(plans, city)
            rides_of = {
                plan.id: orient_rides(by_plan.get(plan.id, []), direction) for plan in plans
            }
        default_plan = plans[0].id if plans else None

        # 2. 걷는 방향의 출발·도착
        home = LatLng(lat=commute.from_lat, lng=commute.from_lng)
        work = LatLng(lat=commute.to_lat, lng=commute.to_lng)
        trip = (home, work) if direction == Direction.TO_WORK else (work, home)

        # 3. 조립 (DB 미접근). 기준선은 같은 계획 것끼리
        baselines: dict[UUID | None, dict[LegType, RankedRoute]] = defaultdict(dict)
        for r in shortest:
            baselines[r.route.ride_id][r.leg_type] = r
        mode = TransportMode(commute.transport_mode)
        result: dict[UUID, list[Segment]] = {}
        for r in [*routes, *shortest]:
            plan_id = r.route.ride_id if r.route.ride_id in rides_of else default_plan
            result[r.route.id] = compose_journey(
                r,
                shortest=baselines.get(plan_id, {}) or baselines.get(None, {}),
                rides=rides_of.get(plan_id, []),
                transport_mode=mode,
                trip=trip,
            )
        return result

    async def _rides(self, plans: list["RidePlanView"], city: str) -> dict[UUID | None, list[Ride]]:
        """계획마다 station_ids [탑승, 하차, ...] 쌍 사이 정류장을 채운 탑승 목록 (출근 방향).

        같은 노선·같은 종류(시드 / ODsay)의 정류장만 순번으로 잇는다. 노선이 다르거나 행이 없으면
        탑승·하차 두 점만 잇는다. 관련 노선의 정류장은 한 번에 읽는다 (운영 DB 왕복이 느리다).
        """
        ends = [sid for plan in plans for sid in plan.station_ids]
        if not ends:
            return {}
        lines = select(Station.line).where(Station.id.in_(ends)).scalar_subquery()
        stmt = select(Station).where(Station.line.in_(lines), Station.city == city)
        stations = list((await self._session.execute(stmt)).scalars())
        by_id = {s.id: s for s in stations}

        def between(board: Station, alight: Station) -> list[Station]:
            seeded = board.external_id is None
            if board.line != alight.line or seeded != (alight.external_id is None):
                return []
            low, high = sorted((board.order_no, alight.order_no))
            found = sorted(
                (
                    s
                    for s in stations
                    if s.line == board.line
                    and (s.external_id is None) == seeded
                    and low < s.order_no < high
                ),
                key=lambda s: s.order_no,
            )
            if board.order_no > alight.order_no:
                found.reverse()
            return found

        result: dict[UUID | None, list[Ride]] = {}
        for plan in plans:
            rides: list[Ride] = []
            ids = plan.station_ids
            for board_id, alight_id in zip(ids[0::2], ids[1::2], strict=False):
                board, alight = by_id.get(board_id), by_id.get(alight_id)
                if board is None or alight is None:
                    continue
                rides.append([board, *between(board, alight), alight])
            result[plan.id] = rides
        return result


@dataclass(frozen=True)
class RidePlanView:
    """경로 생성·journey 가 보는 탑승 계획 (#88). CommuteRide 또는 옛 동선 열에서 만든다."""

    id: UUID | None
    line: str
    station_ids: list[UUID]
    alight_early_station_ids: list[UUID]
    board_late_station_ids: list[UUID]


def ride_plans(commute: Commute) -> list[RidePlanView]:
    """동선의 탑승 계획들. commute_rides 가 없는 옛 동선은 거울 열로 계획 하나를 만든다."""
    rides: list[CommuteRide] = list(commute.rides or [])
    if rides:
        return [
            RidePlanView(
                id=ride.id,
                line=ride.line,
                station_ids=list(ride.station_ids),
                alight_early_station_ids=list(ride.alight_early_station_ids),
                board_late_station_ids=list(ride.board_late_station_ids),
            )
            for ride in rides
        ]
    if not commute.station_ids:
        return []
    return [
        RidePlanView(
            id=None,
            line=commute.line or "",
            station_ids=list(commute.station_ids),
            alight_early_station_ids=list(commute.alight_early_station_ids),
            board_late_station_ids=list(commute.board_late_station_ids),
        )
    ]


def _static_of(route: Route, direction: Direction) -> StaticScore:
    return StaticScore(
        green_pct=route.green_pct,
        water_score=route.water_score,
        landmark_score=route.landmark_score,
        sunset_ratio=route.sunset_ratio_for(direction),
        tags=[PlaceType(value) for value in route.tags],
    )


def _extra_minutes(
    leg: Leg, walked: PedestrianRoute, reference: dict[tuple[UUID | None, LegType], float]
) -> float:
    """원래 계획 대비 더 걸리는 분. 건너뛰기 구간은 덜 탄 정거장만큼 뺀다."""
    base = reference.get(leg.reference_key, 0.0)
    return walked.minutes - base - leg.skip_stops * RIDE_MINUTES_PER_STOP


def _at(station: Station) -> LatLng:
    return LatLng(lat=station.lat, lng=station.lng)


def _route_name(leg: Leg, places: list[Place]) -> str:
    if leg.leg_type == LegType.ALIGHT_EARLY:
        if not places:
            return f"{leg.station_name}에서 내려 걷기"
        suffix = f" ({leg.skip_stops}정거장 먼저)"
    elif leg.leg_type == LegType.BOARD_LATE:
        if not places:
            return f"{leg.station_name}까지 걸어가서 타기"
        suffix = f" ({leg.skip_stops}정거장 걸어가서)"
    elif leg.leg_type == LegType.AFTER_ARRIVAL:
        suffix = " (도착 후)"
    else:
        suffix = ""
    if not places:
        return "최단 경로"
    base = places[0].name.replace(" 진입", "").replace(" 진출", "")
    return f"{base} 샛길{suffix}"


def get_route_service(
    session: AsyncSession = Depends(get_db), tmap: TmapClient = Depends(get_tmap_client)
) -> RouteService:
    return RouteService(session, tmap)
