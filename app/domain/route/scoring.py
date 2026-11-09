"""F-04 녹지 % + 이벤트 점수.

경로선 하나를 50m 간격 점으로 쪼개고, 각 점이 녹지(30m)·하천(50m) 가까이 있는지를 PostGIS 로 센다.
다리·전망은 경로 100m 안의 Place(bridge/view) 로, 노을은 일몰 방위각과 진행 방향의 차이로 본다.
조건값은 전부 이 파일의 상수다 (명세: 규칙 조건값은 코드의 상수).
"""

import math
from datetime import datetime, timedelta, timezone
from typing import Any

from astral import Observer
from astral.sun import azimuth as sun_azimuth
from astral.sun import sun
from geoalchemy2 import Geography, Geometry, WKBElement
from geoalchemy2.shape import from_shape
from pydantic import BaseModel, ConfigDict, Field
from shapely.geometry import LineString
from shapely.ops import substring
from sqlalchemy import cast, column, exists, func, select
from sqlalchemy.dialects.postgresql import array
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import QueryableAttribute
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.selectable import TableValuedAlias

from app.clients.tmap import LatLng
from app.domain.route.models import GreenArea, GreenKind, Place, PlaceType

SAMPLE_STEP_M = 50
GREEN_RADIUS_M = 30
RIVER_RADIUS_M = 50
LANDMARK_RADIUS_M = 100

WATER_WEIGHT = 10
LANDMARK_BONUS = 10
SUNSET_WEIGHT = 10
EVENT_SCORE_MAX = 30

SUNSET_WINDOW = timedelta(minutes=30)
SUNSET_HEADING_TOLERANCE_DEG = 30
# 이 비율 이상이어야 경로의 "성격" 태그로 인정한다 (F-16 가중치용)
TAG_RATIO_THRESHOLD = 0.3

# 왕복 판정 (#49). 10m 간격 점이 30m 이상 앞서 지나온 길에서 8m 안이면 "다시 밟은 길"
RETRACE_STEP_M = 10
RETRACE_TOLERANCE_M = 8
RETRACE_GAP_M = 30

METERS_PER_DEG_LAT = 110_540
METERS_PER_DEG_LNG_AT_EQUATOR = 111_320
BBOX_MARGIN = 1.1  # 인덱스 사전 필터는 넉넉하게. 정확한 판정은 ST_DWithin 이 한다

LANDMARK_TYPES = (PlaceType.BRIDGE.value, PlaceType.VIEW.value)
# 거리 계산용 타입. geography 로 캐스트해야 ST_DWithin 의 단위가 미터가 된다
GEOGRAPHY = Geography(geometry_type="GEOMETRY", srid=4326)
PARK_LIKE_KINDS = {GreenKind.PARK, GreenKind.FOREST, GreenKind.GRASS}
# 저장용 노을 방향 비율은 데모 기간의 일몰 방위각(11/10 대구 약 249°)으로 계산한다.
# 하루 단위 변화는 1° 미만이라 ±30° 허용 폭 안에서 무시된다
_NOON_REFERENCE = datetime(2026, 11, 10, 12, 0, tzinfo=timezone(timedelta(hours=9)))


class StaticScore(BaseModel):
    """경로 자체의 성질. 날짜·날씨와 무관해서 Route 에 저장한다."""

    model_config = ConfigDict(frozen=True)

    green_pct: float = Field(..., ge=0, le=100, description="녹지 30m 안 구간 비율 × 100")
    water_score: float = Field(..., ge=0, le=WATER_WEIGHT, description="하천 50m 안 비율 × 10")
    landmark_score: float = Field(..., ge=0, le=LANDMARK_BONUS, description="다리·전망 +10")
    sunset_ratio: float = Field(..., ge=0, le=1, description="일몰 방향으로 걷는 구간 비율")
    sunset_ratio_reverse: float = Field(
        0.0, ge=0, le=1, description="같은 길을 반대로 걸을 때의 일몰 방향 비율 (#56)"
    )
    tags: list[PlaceType] = Field(..., description="river·park·trees·bridge·view. sunset 은 동적")


class RouteScore(BaseModel):
    """F-03·F-15·F-05 가 쓰는 경로 점수. 정적 점수에 오늘의 노을 창을 적용한 값."""

    model_config = ConfigDict(frozen=True)

    green_pct: float = Field(..., ge=0, le=100, description="녹지 30m 안 구간 비율 × 100")
    event_score: float = Field(..., ge=0, le=EVENT_SCORE_MAX, description="물가·다리·노을 가산")
    tags: list[PlaceType] = Field(..., description="경로의 성격. 피드백 가중치의 키")


def apply_sunset(static: StaticScore, *, window_open: bool) -> RouteScore:
    """정적 점수에 오늘의 노을 창을 적용한다. 창이 열려 있으면 노을 방향 비율 × 10 을 더한다."""
    facing_ratio = static.sunset_ratio if window_open else 0.0
    tags = list(static.tags)
    if facing_ratio >= TAG_RATIO_THRESHOLD:
        tags.append(PlaceType.SUNSET)
    event_score = static.water_score + static.landmark_score + facing_ratio * SUNSET_WEIGHT
    return RouteScore(
        green_pct=static.green_pct,
        event_score=round(min(event_score, EVENT_SCORE_MAX), 1),
        tags=tags,
    )


class SunsetWindow(BaseModel):
    """노을 창. 일몰 ±30분 안이고 맑을 때만 열린다."""

    model_config = ConfigDict(frozen=True)

    is_open: bool
    azimuth_deg: float = Field(..., description="일몰 방위각 (북 0°, 시계 방향)")
    sunset_at: datetime


# --- 순수 계산 (DB 없음) -----------------------------------------------------


def meters_per_deg_lng(lat: float) -> float:
    return METERS_PER_DEG_LNG_AT_EQUATOR * math.cos(math.radians(lat))


def _to_local_meters(polyline: list[LatLng]) -> tuple[LineString, LatLng, float]:
    """위경도 좌표열을 첫 점 기준 평면 미터 좌표로 바꾼다. 도시 규모에서는 오차가 무시된다."""
    origin = polyline[0]
    scale_lng = meters_per_deg_lng(origin.lat)
    line = LineString(
        [
            ((p.lng - origin.lng) * scale_lng, (p.lat - origin.lat) * METERS_PER_DEG_LAT)
            for p in polyline
        ]
    )
    return line, origin, scale_lng


def retrace_m(polyline: list[LatLng]) -> float:
    """
    경로 중 이미 지나온 길을 다시 밟는 길이 (m).

    공원 안쪽 경유지로 들어갔다 같은 길로 나오는 TMAP 경로를 거르는 데 쓴다.
    바로 직전 구간(RETRACE_GAP_M)은 비교에서 빼서, 꺾이는 모퉁이를 왕복으로 세지 않는다.
    """
    line, _, _ = _to_local_meters(polyline)
    # 10m 간격 점마다, 30m 이상 앞의 지나온 길에서 8m 안이면 다시 밟은 10m 로 센다
    retraced = 0
    distance = RETRACE_GAP_M + RETRACE_STEP_M
    while distance <= line.length:
        walked = substring(line, 0, distance - RETRACE_GAP_M)
        if walked.distance(line.interpolate(distance)) < RETRACE_TOLERANCE_M:
            retraced += 1
        distance += RETRACE_STEP_M
    return float(retraced * RETRACE_STEP_M)


def _from_local(x: float, y: float, origin: LatLng, scale_lng: float) -> LatLng:
    return LatLng(lat=origin.lat + y / METERS_PER_DEG_LAT, lng=origin.lng + x / scale_lng)


def sample_points(polyline: list[LatLng], step_m: float = SAMPLE_STEP_M) -> list[LatLng]:
    """경로선 위에 step_m 간격 점을 찍는다. 시작점과 끝점을 항상 포함한다."""
    if len(polyline) < 2:
        raise ValueError("경로선은 점이 2개 이상이어야 한다")
    line, origin, scale_lng = _to_local_meters(polyline)
    distances: list[float] = []
    current = 0.0
    while current < line.length - 1e-6:
        distances.append(current)
        current += step_m
    distances.append(line.length)
    return [
        _from_local(point.x, point.y, origin, scale_lng)
        for point in (line.interpolate(d) for d in distances)
    ]


def heading_deg(a: LatLng, b: LatLng) -> float:
    """a → b 진행 방향. 북 0°, 동 90°."""
    dx = (b.lng - a.lng) * meters_per_deg_lng(a.lat)
    dy = (b.lat - a.lat) * METERS_PER_DEG_LAT
    return math.degrees(math.atan2(dx, dy)) % 360


def _angle_diff(a: float, b: float) -> float:
    diff = abs(a - b) % 360
    return min(diff, 360 - diff)


def sunset_ratio(polyline: list[LatLng], azimuth_deg: float) -> float:
    """진행 방향이 일몰 방위각 ±30° 안인 구간의 길이 비율."""
    line, _, _ = _to_local_meters(polyline)
    if line.length == 0:
        return 0.0
    facing = 0.0
    for a, b in zip(polyline, polyline[1:], strict=False):
        segment_len = _to_local_meters([a, b])[0].length
        if _angle_diff(heading_deg(a, b), azimuth_deg) <= SUNSET_HEADING_TOLERANCE_DEG:
            facing += segment_len
    return float(facing / line.length)


def sunset_window(at: datetime, *, lat: float, lng: float, is_clear: bool | None) -> SunsetWindow:
    """
    노을 창을 계산한다.

    :param at: 걷는 시각. tz-aware 여야 한다
    :param is_clear: 맑음 여부. None(날씨 모름)이면 창을 닫힌 것으로 본다
    :raises ValueError: at 에 시간대가 없음
    """
    if at.tzinfo is None:
        raise ValueError("at 은 시간대가 있어야 한다")
    observer = Observer(latitude=lat, longitude=lng)
    sunset_at: datetime = sun(observer, date=at.date(), tzinfo=at.tzinfo)["sunset"]
    is_open = bool(is_clear) and abs(at - sunset_at) <= SUNSET_WINDOW
    return SunsetWindow(
        is_open=is_open, azimuth_deg=float(sun_azimuth(observer, sunset_at)), sunset_at=sunset_at
    )


# --- DB 질의 ---------------------------------------------------------------


def bbox_near(
    column: QueryableAttribute[WKBElement] | ColumnElement[Any],
    target: ColumnElement[Any] | WKBElement,
    radius_m: float,
    lat: float,
) -> ColumnElement[bool]:
    """
    GIST 인덱스로 먼저 거르는 조건 (#54). ST_DWithin(geography) 앞에 붙인다.

    geography 로 캐스트하면 geom 의 GIST 인덱스를 못 쓴다. 대상의 바운딩 박스를 반경만큼 넓혀
    겹치는 행만 남긴 뒤 정확한 거리를 잰다. 경도 1도가 위도 1도보다 짧으므로 경도 기준으로 넓히면
    위도 방향도 빠짐없이 덮는다.
    """
    degrees = radius_m * BBOX_MARGIN / meters_per_deg_lng(lat)
    return column.op("&&")(func.ST_Expand(target, degrees))


def _sample_table(points: list[LatLng]) -> TableValuedAlias:
    """점 배열을 unnest 해 행 집합(컬럼 pt)으로 쓴다. 한 쿼리로 모든 점을 검사하기 위해서다."""
    geoms = array([func.ST_SetSRID(func.ST_MakePoint(p.lng, p.lat), 4326) for p in points])
    # render_derived 가 "AS samples(pt)" 로 컬럼 이름까지 렌더링한다
    return func.unnest(geoms).table_valued(column("pt", Geometry)).render_derived(name="samples")


class ScoringService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def score_static(self, polyline: list[LatLng], *, city: str) -> StaticScore:
        """
        경로 자체의 성질을 계산한다 (날짜·날씨 무관). F-03 이 Route 에 저장한다.

        :param polyline: 경로 좌표열 (TMAP 응답)
        :param city: 도시 코드. 녹지·Place 조회 범위
        """
        # 1. 50m 간격 점
        points = sample_points(polyline)
        total = len(points)

        # 2·3. 녹지 %(30m 안 green_areas 가 있는 점의 비율)와 물가(50m 안 하천 점의 비율 × 10).
        #      한 질의로 센다. 운영 DB 왕복이 느려 경로마다 질의 수가 생성 시간을 정한다 (#93)
        covered_kinds, green_hits, river_hits = await self._green_summary(points, city)
        green_pct = round(green_hits / total * 100, 1)
        water_ratio = river_hits / total

        # 4. 다리·전망: 경로 100m 안의 Place(bridge/view) 가 있으면 +10
        landmark_types = await self._landmark_types(polyline, city)

        # 5. 노을 방향 비율. 창이 열렸는지는 요청 때 apply_sunset 이 본다
        azimuth = sunset_window(
            _NOON_REFERENCE, lat=polyline[0].lat, lng=polyline[0].lng, is_clear=None
        ).azimuth_deg
        facing_ratio = sunset_ratio(polyline, azimuth)
        facing_ratio_reverse = sunset_ratio(list(reversed(polyline)), azimuth)

        # 6. 태그: 비율이 기준을 넘는 성격만
        tags: list[PlaceType] = []
        if water_ratio >= TAG_RATIO_THRESHOLD:
            tags.append(PlaceType.RIVER)
        if covered_kinds & PARK_LIKE_KINDS and green_pct >= TAG_RATIO_THRESHOLD * 100:
            tags.append(PlaceType.PARK)
        if GreenKind.TREES in covered_kinds and green_pct >= TAG_RATIO_THRESHOLD * 100:
            tags.append(PlaceType.TREES)
        tags.extend(PlaceType(value) for value in LANDMARK_TYPES if value in landmark_types)

        return StaticScore(
            green_pct=green_pct,
            water_score=round(water_ratio * WATER_WEIGHT, 1),
            landmark_score=LANDMARK_BONUS if landmark_types else 0,
            sunset_ratio=round(facing_ratio, 3),
            sunset_ratio_reverse=round(facing_ratio_reverse, 3),
            tags=tags,
        )

    async def score(
        self,
        polyline: list[LatLng],
        *,
        city: str,
        at: datetime,
        is_clear: bool | None,
    ) -> RouteScore:
        """
        경로 하나의 녹지 % 와 이벤트 점수를 한 번에 계산한다 (정적 점수 + 오늘의 노을 창).

        :param at: 걷는 시각 (tz-aware). 노을 창 판정
        :param is_clear: 맑음 여부. None 이면 노을 가산 없음
        """
        static = await self.score_static(polyline, city=city)
        window = sunset_window(at, lat=polyline[0].lat, lng=polyline[0].lng, is_clear=is_clear)
        return apply_sunset(static, window_open=window.is_open)

    async def _green_summary(
        self, points: list[LatLng], city: str
    ) -> tuple[set[GreenKind], int, int]:
        """점마다 30m 안 녹지 종류와 50m 안 하천 유무를 한 번에 센다.

        :return: (녹지 종류 집합, 녹지가 있는 점 수, 하천이 있는 점 수)
        """
        samples = _sample_table(points)
        lat = points[0].lat
        kinds_near = (
            select(func.array_agg(func.distinct(GreenArea.kind)))
            .select_from(GreenArea)
            .where(
                GreenArea.city == city,
                bbox_near(GreenArea.geom, samples.c.pt, GREEN_RADIUS_M, lat),
                func.ST_DWithin(
                    cast(GreenArea.geom, GEOGRAPHY), cast(samples.c.pt, GEOGRAPHY), GREEN_RADIUS_M
                ),
            )
            .scalar_subquery()
        )
        river_near = (
            select(1)
            .select_from(GreenArea)
            .where(
                GreenArea.city == city,
                GreenArea.kind == GreenKind.RIVER.value,
                bbox_near(GreenArea.geom, samples.c.pt, RIVER_RADIUS_M, lat),
                func.ST_DWithin(
                    cast(GreenArea.geom, GEOGRAPHY), cast(samples.c.pt, GEOGRAPHY), RIVER_RADIUS_M
                ),
            )
        )
        stmt = select(kinds_near.label("kinds"), exists(river_near).label("river")).select_from(
            samples
        )
        rows = (await self._session.execute(stmt)).all()
        covered: set[GreenKind] = set()
        green_hits = river_hits = 0
        for kinds, river in rows:
            if kinds:
                green_hits += 1
                covered.update(GreenKind(value) for value in kinds)
            if river:
                river_hits += 1
        return covered, green_hits, river_hits

    async def _count_near(
        self, points: list[LatLng], city: str, radius_m: float, *, kinds: list[GreenKind] | None
    ) -> int:
        """radius_m 안에 green_areas 가 하나라도 있는 점의 수."""
        samples = _sample_table(points)
        near = (
            select(1)
            .select_from(GreenArea)
            .where(
                GreenArea.city == city,
                bbox_near(GreenArea.geom, samples.c.pt, radius_m, points[0].lat),
                func.ST_DWithin(
                    cast(GreenArea.geom, GEOGRAPHY), cast(samples.c.pt, GEOGRAPHY), radius_m
                ),
            )
        )
        if kinds is not None:
            near = near.where(GreenArea.kind.in_([kind.value for kind in kinds]))
        stmt = select(func.count()).select_from(samples).where(exists(near))
        return int((await self._session.execute(stmt)).scalar_one())

    async def _covered_kinds(
        self, points: list[LatLng], city: str, radius_m: float
    ) -> set[GreenKind]:
        """점들 가까이에 있는 녹지 종류 집합 (태그용)."""
        samples = _sample_table(points)
        near_any_point = (
            select(1)
            .select_from(samples)
            .where(
                bbox_near(GreenArea.geom, samples.c.pt, radius_m, points[0].lat),
                func.ST_DWithin(
                    cast(GreenArea.geom, GEOGRAPHY), cast(samples.c.pt, GEOGRAPHY), radius_m
                ),
            )
        )
        stmt = (
            select(GreenArea.kind).distinct().where(GreenArea.city == city, exists(near_any_point))
        )
        rows = (await self._session.execute(stmt)).scalars()
        return {GreenKind(value) for value in rows}

    async def _landmark_types(self, polyline: list[LatLng], city: str) -> set[str]:
        """경로 100m 안 Place 의 bridge/view 태그 집합."""
        line = from_shape(LineString([(p.lng, p.lat) for p in polyline]), srid=4326)
        stmt = select(func.unnest(Place.types).label("place_type")).where(
            Place.city == city,
            Place.types.overlap(list(LANDMARK_TYPES)),
            bbox_near(Place.location, line, LANDMARK_RADIUS_M, polyline[0].lat),
            func.ST_DWithin(
                cast(Place.location, GEOGRAPHY), cast(line, GEOGRAPHY), LANDMARK_RADIUS_M
            ),
        )
        rows = (await self._session.execute(stmt)).scalars()
        return {value for value in rows if value in LANDMARK_TYPES}
