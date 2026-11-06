"""OSM 녹지 → 자동 Place 추출 기대 동작. 가짜 폴리곤·선을 테스트 트랜잭션 안에 넣는다."""

from geoalchemy2.shape import from_shape
from shapely.geometry import LineString, Point, Polygon
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.route.models import GreenArea, GreenKind, Place, PlaceType
from scripts.derive_places import SOURCE_CURATED, SOURCE_OSM, derive_places

CITY = "testcity"
LAT, LNG = 35.86, 128.60
M_LAT = 1 / 110_540  # 1m 의 위도
M_LNG = 1 / 90_200  # 위도 35.86 에서 1m 의 경도


def square(lat: float, lng: float, side_m: float) -> Polygon:
    d_lat, d_lng = side_m * M_LAT / 2, side_m * M_LNG / 2
    return Polygon(
        [
            (lng - d_lng, lat - d_lat),
            (lng + d_lng, lat - d_lat),
            (lng + d_lng, lat + d_lat),
            (lng - d_lng, lat + d_lat),
        ]
    )


def green(kind: GreenKind, name: str | None, geom: Polygon | LineString) -> GreenArea:
    return GreenArea(kind=kind.value, name=name, geom=from_shape(geom, srid=4326), city=CITY)


async def places(session: AsyncSession) -> list[Place]:
    return list((await session.execute(select(Place).where(Place.city == CITY))).scalars())


async def test_named_park_becomes_place_and_unnamed_or_tiny_is_skipped(
    db_session: AsyncSession,
) -> None:
    db_session.add_all(
        [
            green(GreenKind.PARK, "범어공원", square(LAT, LNG, 100)),  # 1ha
            green(GreenKind.PARK, None, square(LAT + 0.01, LNG, 100)),  # 이름 없음
            green(GreenKind.PARK, "작은공원", square(LAT + 0.02, LNG, 30)),  # 0.09ha
            green(GreenKind.FOREST, "앞산숲", square(LAT + 0.03, LNG, 100)),
        ]
    )
    await db_session.flush()

    result = await derive_places(db_session, CITY)

    rows = {p.name: p for p in await places(db_session)}
    assert set(rows) == {"범어공원", "앞산숲"}
    assert rows["범어공원"].place_types == [PlaceType.PARK]
    assert rows["범어공원"].source == SOURCE_OSM
    assert rows["범어공원"].description == "범어공원을 가로지르는 길"
    assert rows["앞산숲"].place_types == [PlaceType.TREES, PlaceType.PARK]
    assert result.upserted == 2


async def test_big_park_gets_boundary_entrances(db_session: AsyncSession) -> None:
    db_session.add(green(GreenKind.PARK, "두류공원", square(LAT, LNG, 400)))  # 16ha, 둘레 1.6km
    await db_session.flush()

    await derive_places(db_session, CITY)

    names = sorted(p.name for p in await places(db_session))
    assert "두류공원" in names
    entrances = [n for n in names if "입구" in n]
    assert len(entrances) == 3  # 1600m // 500m
    assert all(n.startswith("두류공원 ") and n.endswith("쪽 입구") for n in entrances)


async def test_river_line_becomes_entry_exit_pairs(db_session: AsyncSession) -> None:
    # 동서 2km 하천 선 → 800m 간격 점 3개 (0·800·1600) → 쌍 2개
    river = LineString([(LNG, LAT), (LNG + 2000 * M_LNG, LAT)])
    db_session.add(green(GreenKind.RIVER, "신천", river))
    await db_session.flush()

    await derive_places(db_session, CITY)

    rows = await places(db_session)
    pairs = {p.pair_id for p in rows if p.pair_id}
    assert len(rows) == 4
    assert len(pairs) == 2
    assert all(p.place_types == [PlaceType.RIVER] for p in rows)
    for pair in pairs:
        assert sum(1 for p in rows if p.pair_id == pair) == 2


async def test_candidate_near_curated_place_is_skipped(db_session: AsyncSession) -> None:
    db_session.add(
        Place(
            name="범어공원",
            location=from_shape(Point(LNG, LAT), srid=4326),
            types=[PlaceType.PARK.value],
            description="사람이 쓴 설명",
            city=CITY,
            source=SOURCE_CURATED,
        )
    )
    db_session.add(green(GreenKind.PARK, "범어공원", square(LAT + 50 * M_LAT, LNG, 100)))  # 50m 옆
    await db_session.flush()

    result = await derive_places(db_session, CITY)

    rows = await places(db_session)
    assert len(rows) == 1
    assert rows[0].description == "사람이 쓴 설명"
    assert result.skipped_near_curated == 1


async def test_duplicate_names_get_numbers_and_rerun_is_idempotent(
    db_session: AsyncSession,
) -> None:
    db_session.add_all(
        [
            green(GreenKind.PARK, "어린이공원", square(LAT, LNG, 100)),
            green(GreenKind.PARK, "어린이공원", square(LAT + 0.01, LNG, 100)),
        ]
    )
    await db_session.flush()

    await derive_places(db_session, CITY)
    await derive_places(db_session, CITY)

    names = sorted(p.name for p in await places(db_session))
    assert names == ["어린이공원", "어린이공원 2"]
