"""F-04 녹지 % + 이벤트 점수 기대 동작.

작은 고정 폴리곤·선을 테스트 트랜잭션 안에 넣고 돌린다. OSM 데이터가 없어도 통과한다.
"""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from geoalchemy2.shape import from_shape
from shapely.geometry import LineString, Point, Polygon
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.tmap import LatLng
from app.domain.route.models import GreenArea, GreenKind, Place, PlaceType
from app.domain.route.scoring import (
    RouteScore,
    ScoringService,
    heading_deg,
    meters_per_deg_lng,
    retrace_m,
    sample_points,
    sunset_ratio,
    sunset_window,
)

CITY = "testcity"
KST = timezone(timedelta(hours=9))
LAT = 35.86
LNG0 = 128.60
# 위도 35.86 에서 경도 0.001° ≈ 90m, 위도 0.001° ≈ 110m
DEG_PER_100M_LNG = 0.00111
DEG_PER_100M_LAT = 0.00090

# 11/10 대구 일몰은 17:22 KST 근처, 방위각 약 249°
SUNSET_EVENING = datetime(2026, 11, 10, 17, 20, tzinfo=KST)
NOON = datetime(2026, 11, 10, 12, 0, tzinfo=KST)


def east_line(length_m: float, lat: float = LAT) -> list[LatLng]:
    return [
        LatLng(lat=lat, lng=LNG0),
        LatLng(lat=lat, lng=LNG0 + DEG_PER_100M_LNG * length_m / 100),
    ]


def exact_east_line(length_m: float) -> list[LatLng]:
    """scoring 과 같은 투영 상수로 만든, 길이가 정확히 length_m 인 선."""
    return [
        LatLng(lat=LAT, lng=LNG0),
        LatLng(lat=LAT, lng=LNG0 + length_m / meters_per_deg_lng(LAT)),
    ]


def west_line(length_m: float) -> list[LatLng]:
    return list(reversed(east_line(length_m)))


def box(lng_from_m: float, lng_to_m: float, lat_margin_m: float = 60) -> Polygon:
    """동서 lng_from_m~lng_to_m, 남북 ±lat_margin_m 사각형."""
    x0 = LNG0 + DEG_PER_100M_LNG * lng_from_m / 100
    x1 = LNG0 + DEG_PER_100M_LNG * lng_to_m / 100
    dy = DEG_PER_100M_LAT * lat_margin_m / 100
    return Polygon([(x0, LAT - dy), (x1, LAT - dy), (x1, LAT + dy), (x0, LAT + dy)])


async def add_green(session: AsyncSession, kind: GreenKind, geom: Polygon | LineString) -> None:
    session.add(GreenArea(kind=kind.value, geom=from_shape(geom, srid=4326), city=CITY))
    await session.flush()


async def add_place(
    session: AsyncSession, name: str, types: list[PlaceType], lat: float, lng: float
) -> None:
    session.add(
        Place(
            name=name,
            location=from_shape(Point(lng, lat), srid=4326),
            types=[t.value for t in types],
            description="테스트 지점",
            city=CITY,
        )
    )
    await session.flush()


async def score(
    session: AsyncSession, line: list[LatLng], at: datetime = NOON, is_clear: bool | None = None
) -> RouteScore:
    return await ScoringService(session).score(line, city=CITY, at=at, is_clear=is_clear)


# --- 순수 계산 ----------------------------------------------------------------


def test_sample_points_every_50m_including_ends() -> None:
    line = exact_east_line(1000)

    points = sample_points(line)

    assert len(points) == 21
    assert points[0] == line[0]
    assert points[-1].lng == pytest.approx(line[1].lng, abs=1e-7)


def test_sample_points_keeps_the_end_when_length_is_not_a_multiple() -> None:
    assert len(sample_points(exact_east_line(120))) == 4  # 0, 50, 100, 120


def test_sample_points_requires_two_points() -> None:
    with pytest.raises(ValueError):
        sample_points([LatLng(lat=LAT, lng=LNG0)])


def test_heading_east_is_90_degrees() -> None:
    a, b = east_line(100)

    assert heading_deg(a, b) == pytest.approx(90, abs=0.5)
    assert heading_deg(b, a) == pytest.approx(270, abs=0.5)


def test_sunset_window_opens_only_near_sunset_when_clear() -> None:
    evening = sunset_window(SUNSET_EVENING, lat=LAT, lng=LNG0, is_clear=True)
    cloudy = sunset_window(SUNSET_EVENING, lat=LAT, lng=LNG0, is_clear=False)
    unknown = sunset_window(SUNSET_EVENING, lat=LAT, lng=LNG0, is_clear=None)
    noon = sunset_window(NOON, lat=LAT, lng=LNG0, is_clear=True)

    assert evening.is_open is True
    assert 230 <= evening.azimuth_deg <= 260
    assert cloudy.is_open is False
    assert unknown.is_open is False
    assert noon.is_open is False


def test_sunset_window_requires_timezone() -> None:
    with pytest.raises(ValueError):
        sunset_window(datetime(2026, 11, 10, 17, 20), lat=LAT, lng=LNG0, is_clear=True)


def test_sunset_ratio_counts_segments_facing_the_sun() -> None:
    assert sunset_ratio(west_line(300), azimuth_deg=250) == pytest.approx(1.0)
    assert sunset_ratio(east_line(300), azimuth_deg=250) == pytest.approx(0.0)


# --- 녹지 % ----------------------------------------------------------------------


async def test_line_inside_park_scores_100_percent(db_session: AsyncSession) -> None:
    await add_green(db_session, GreenKind.PARK, box(-50, 150))

    result = await score(db_session, east_line(100))

    assert result.green_pct == 100
    assert PlaceType.PARK in result.tags


async def test_line_without_green_scores_0_percent(db_session: AsyncSession) -> None:
    result = await score(db_session, east_line(100))

    assert result.green_pct == 0
    assert result.event_score == 0
    assert result.tags == []


async def test_half_covered_line_scores_about_half(db_session: AsyncSession) -> None:
    # 300m 선, 점 7개(0~300). 폴리곤이 0~150m 를 덮으면 150m 점까지 4개가 30m 안
    await add_green(db_session, GreenKind.GRASS, box(-50, 150))

    result = await score(db_session, east_line(300))

    assert 40 <= result.green_pct <= 70


async def test_far_green_does_not_count(db_session: AsyncSession) -> None:
    # 남쪽으로 200m 떨어진 공원
    far_south = LAT - DEG_PER_100M_LAT * 2
    await add_green(
        db_session,
        GreenKind.PARK,
        Polygon(
            [
                (LNG0 - 0.001, far_south - 0.0005),
                (LNG0 + 0.002, far_south - 0.0005),
                (LNG0 + 0.002, far_south),
                (LNG0 - 0.001, far_south),
            ]
        ),
    )

    result = await score(db_session, east_line(100))

    assert result.green_pct == 0


# --- 이벤트 점수 ------------------------------------------------------------------


async def test_river_line_20m_away_gives_water_score(db_session: AsyncSession) -> None:
    river_lat = LAT + DEG_PER_100M_LAT * 0.2
    river = LineString([(LNG0 - 0.001, river_lat), (LNG0 + 0.003, river_lat)])
    await add_green(db_session, GreenKind.RIVER, river)

    result = await score(db_session, east_line(200))

    assert result.event_score == pytest.approx(10)
    assert PlaceType.RIVER in result.tags
    # 하천도 녹지라 30m 안이면 녹지 % 에도 잡힌다
    assert result.green_pct == 100


async def test_bridge_place_within_100m_adds_10(db_session: AsyncSession) -> None:
    await add_place(db_session, "테스트교", [PlaceType.BRIDGE], LAT + DEG_PER_100M_LAT * 0.5, LNG0)

    result = await score(db_session, east_line(100))

    assert result.event_score == pytest.approx(10)
    assert result.tags == [PlaceType.BRIDGE]


async def test_bridge_place_300m_away_does_not_count(db_session: AsyncSession) -> None:
    await add_place(db_session, "먼다리", [PlaceType.BRIDGE], LAT + DEG_PER_100M_LAT * 3, LNG0)

    result = await score(db_session, east_line(100))

    assert result.event_score == 0


async def test_walking_west_at_sunset_when_clear_adds_sunset_score(
    db_session: AsyncSession,
) -> None:
    result = await score(db_session, west_line(300), at=SUNSET_EVENING, is_clear=True)

    assert result.event_score == pytest.approx(10)
    assert result.tags == [PlaceType.SUNSET]


async def test_sunset_score_is_zero_when_cloudy_or_noon(db_session: AsyncSession) -> None:
    cloudy = await score(db_session, west_line(300), at=SUNSET_EVENING, is_clear=False)
    noon = await score(db_session, west_line(300), at=NOON, is_clear=True)

    assert cloudy.event_score == 0
    assert noon.event_score == 0


async def test_event_score_is_capped_at_30(db_session: AsyncSession) -> None:
    river_lat = LAT + DEG_PER_100M_LAT * 0.2
    await add_green(
        db_session,
        GreenKind.RIVER,
        LineString([(LNG0 - 0.01, river_lat), (LNG0 + 0.01, river_lat)]),
    )
    await add_place(db_session, "테스트교", [PlaceType.BRIDGE, PlaceType.VIEW], LAT, LNG0)

    result = await score(db_session, west_line(300), at=SUNSET_EVENING, is_clear=True)

    assert result.event_score == 30
    assert set(result.tags) == {PlaceType.RIVER, PlaceType.BRIDGE, PlaceType.VIEW, PlaceType.SUNSET}


def test_utc_datetime_is_accepted() -> None:
    window = sunset_window(SUNSET_EVENING.astimezone(UTC), lat=LAT, lng=LNG0, is_clear=True)

    assert window.is_open is True


# --- 왕복 (#49) ---------------------------------------------------------------


def north(point: LatLng, meters: float) -> LatLng:
    return LatLng(lat=point.lat + meters / 110_540, lng=point.lng)


def test_straight_route_has_no_retrace() -> None:
    assert retrace_m(exact_east_line(1000)) == 0


def test_v_shaped_detour_has_no_retrace() -> None:
    # 공원을 꼭짓점으로 돌아가는 V 자 경로는 같은 길을 다시 밟지 않는다
    start, end = exact_east_line(1000)
    apex = north(LatLng(lat=start.lat, lng=(start.lng + end.lng) / 2), 300)

    assert retrace_m([start, apex, end]) <= 20


def test_spur_into_park_and_back_counts_the_return() -> None:
    # 출발 → 북쪽 300m 공원 안 → 같은 길로 출발점 복귀 → 동쪽 1km 도착.
    # 되돌아오는 300m 중 꺾인 직후 30m 를 뺀 만큼이 다시 밟은 길이다
    start, end = exact_east_line(1000)
    park = north(start, 300)

    assert 250 <= retrace_m([start, park, start, end]) <= 300
