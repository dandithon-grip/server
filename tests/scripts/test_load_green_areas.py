"""OSM 녹지 적재 기대 동작.

osm2pgsql 이 만드는 planet_osm_polygon · planet_osm_line 을 테스트 트랜잭션 안에서 직접 만들고,
적재 SQL 이 태그를 kind 로 바꾸고 bbox 밖을 버리고 두 번 돌려도 같은지 본다.
실제 OSM 추출이 적재된 DB 에서만 되는 확인은 integration 마커로 따로 둔다.
"""

import csv
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from geoalchemy2 import Geography
from sqlalchemy import cast, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.domain.route.models import GreenArea, GreenKind
from scripts.load_green_areas import BBOX_CSV, CityBBox, LoadError, load_green_areas, read_bbox

PLACES_CSV = Path("data/places_daegu.csv")
GEOGRAPHY = Geography(geometry_type="GEOMETRY", srid=4326)

# 대구 도심 한 조각. 적재 SQL 만 보기 위한 작은 범위다
TEST_BBOX = CityBBox(city="testcity", min_lng=128.58, min_lat=35.83, max_lng=128.62, max_lat=35.89)

POLYGON_DDL = """
CREATE TABLE planet_osm_polygon (
    osm_id bigint,
    leisure text,
    landuse text,
    "natural" text,
    waterway text,
    building text,
    name text,
    way geometry(Geometry, 4326)
)
"""
LINE_DDL = """
CREATE TABLE planet_osm_line (
    osm_id bigint,
    waterway text,
    "natural" text,
    highway text,
    name text,
    way geometry(Geometry, 4326)
)
"""

# bbox 안의 작은 사각형 (경상감영공원 근처)
IN_BBOX_SQUARE = (
    "POLYGON((128.591 35.871, 128.593 35.871, 128.593 35.873, 128.591 35.873, 128.591 35.871))"
)
# 같은 크기의 사각형을 남서쪽으로 옮긴 것 (재적재 때 갱신 확인용)
MOVED_SQUARE = (
    "POLYGON((128.600 35.850, 128.602 35.850, 128.602 35.852, 128.600 35.852, 128.600 35.850))"
)
# bbox 밖 (대구 북쪽 밖)
OUT_BBOX_SQUARE = (
    "POLYGON((128.591 36.500, 128.593 36.500, 128.593 36.502, 128.591 36.502, 128.591 36.500))"
)
# 약 2,500m2 짜리 작은 물웅덩이. 공원 안 연못이 물가로 잡히면 안 된다
SMALL_POND = (
    "POLYGON((128.5915 35.8720, 128.59155 35.8720, 128.59155 35.87205, "
    "128.5915 35.87205, 128.5915 35.8720))"
)
# bbox 안의 선 (신천 방향)
IN_BBOX_LINE = "LINESTRING(128.606 35.835, 128.608 35.861)"


async def insert_polygon(
    session: AsyncSession,
    osm_id: int,
    wkt: str = IN_BBOX_SQUARE,
    *,
    leisure: str | None = None,
    landuse: str | None = None,
    natural: str | None = None,
    building: str | None = None,
) -> None:
    await session.execute(
        text(
            'INSERT INTO planet_osm_polygon (osm_id, leisure, landuse, "natural", building, way) '
            "VALUES (:osm_id, :leisure, :landuse, :natural, :building, "
            "ST_GeomFromText(:wkt, 4326))"
        ),
        {
            "osm_id": osm_id,
            "leisure": leisure,
            "landuse": landuse,
            "natural": natural,
            "building": building,
            "wkt": wkt,
        },
    )


async def insert_line(
    session: AsyncSession,
    osm_id: int,
    wkt: str = IN_BBOX_LINE,
    *,
    waterway: str | None = None,
    natural: str | None = None,
    highway: str | None = None,
) -> None:
    await session.execute(
        text(
            'INSERT INTO planet_osm_line (osm_id, waterway, "natural", highway, way) '
            "VALUES (:osm_id, :waterway, :natural, :highway, ST_GeomFromText(:wkt, 4326))"
        ),
        {
            "osm_id": osm_id,
            "waterway": waterway,
            "natural": natural,
            "highway": highway,
            "wkt": wkt,
        },
    )


@pytest.fixture
async def osm_tables(db_session: AsyncSession) -> AsyncIterator[AsyncSession]:
    """osm2pgsql 출력 테이블 두 개를 만든다. 테스트가 끝나면 트랜잭션과 함께 사라진다."""
    await db_session.execute(text(POLYGON_DDL))
    await db_session.execute(text(LINE_DDL))
    yield db_session


async def geometry_type(session: AsyncSession, kind: GreenKind) -> str:
    stmt = select(func.GeometryType(GreenArea.geom)).where(GreenArea.kind == kind.value)
    return str((await session.execute(stmt)).scalars().first())


async def count_near(
    session: AsyncSession, kind: GreenKind, *, lat: float, lng: float, radius_m: float = 50
) -> int:
    """좌표 radius_m 안에 있는 그 종류의 녹지 행 수."""
    point = func.ST_SetSRID(func.ST_MakePoint(lng, lat), 4326)
    stmt = (
        select(func.count())
        .select_from(GreenArea)
        .where(
            GreenArea.city == "daegu",
            GreenArea.kind == kind.value,
            func.ST_DWithin(cast(GreenArea.geom, GEOGRAPHY), cast(point, GEOGRAPHY), radius_m),
        )
    )
    return int((await session.execute(stmt)).scalar_one())


async def test_osm_tags_become_green_kinds(osm_tables: AsyncSession) -> None:
    session = osm_tables
    await insert_polygon(session, 1, leisure="park")
    await insert_polygon(session, 2, natural="wood")
    await insert_polygon(session, 3, landuse="forest")
    await insert_polygon(session, 4, landuse="grass")
    await insert_polygon(session, 5, natural="water")
    await insert_polygon(session, 6, building="yes")
    await insert_line(session, 7, waterway="river")
    await insert_line(session, 8, natural="tree_row")
    await insert_line(session, 9, highway="primary")

    counts = await load_green_areas(session, TEST_BBOX)

    assert counts == {
        GreenKind.PARK.value: 1,
        GreenKind.FOREST.value: 2,
        GreenKind.GRASS.value: 1,
        # natural=water 면과 waterway=river 선이 둘 다 물가다
        GreenKind.RIVER.value: 2,
        GreenKind.TREES.value: 1,
    }


async def test_area_is_multipolygon_and_line_is_multilinestring(osm_tables: AsyncSession) -> None:
    session = osm_tables
    await insert_polygon(session, 1, leisure="park")
    await insert_line(session, 2, natural="tree_row")

    await load_green_areas(session, TEST_BBOX)

    # green_areas 의 CHECK 가 면·선만 받는다. 단일 POLYGON 이 아니라 MULTI 로 넣는다
    assert await geometry_type(session, GreenKind.PARK) == "MULTIPOLYGON"
    assert await geometry_type(session, GreenKind.TREES) == "MULTILINESTRING"


async def test_feature_outside_bbox_is_skipped(osm_tables: AsyncSession) -> None:
    session = osm_tables
    await insert_polygon(session, 1, OUT_BBOX_SQUARE, leisure="park")

    counts = await load_green_areas(session, TEST_BBOX)

    assert counts == {}


async def test_city_comes_from_bbox_row(osm_tables: AsyncSession) -> None:
    session = osm_tables
    await insert_polygon(session, 1, leisure="park")

    await load_green_areas(session, TEST_BBOX)

    area = (await session.execute(select(GreenArea))).scalars().one()
    assert area.city == TEST_BBOX.city
    assert area.osm_id == 1


async def test_small_pond_is_not_a_waterside(osm_tables: AsyncSession) -> None:
    session = osm_tables
    # 공원 안 작은 연못(2,500m2)은 물가가 아니다. 호수(40,000m2)는 물가다
    await insert_polygon(session, 1, SMALL_POND, natural="water")
    await insert_polygon(session, 2, IN_BBOX_SQUARE, natural="water")

    counts = await load_green_areas(session, TEST_BBOX)

    assert counts == {GreenKind.RIVER.value: 1}
    area = (await session.execute(select(GreenArea))).scalars().one()
    assert area.osm_id == 2


async def test_same_way_with_two_green_tags_becomes_one_row(osm_tables: AsyncSession) -> None:
    session = osm_tables
    # landuse=forest 와 natural=wood 가 같이 붙은 면. kind 가 같아 한 행이어야 한다
    await insert_polygon(session, 1, landuse="forest", natural="wood")

    counts = await load_green_areas(session, TEST_BBOX)

    assert counts == {GreenKind.FOREST.value: 1}


async def test_loading_twice_updates_geometry_without_duplicating(
    osm_tables: AsyncSession,
) -> None:
    session = osm_tables
    await insert_polygon(session, 1, leisure="park")
    await load_green_areas(session, TEST_BBOX)

    # 같은 osm_id 의 면이 OSM 에서 수정된 상황
    await session.execute(text("DELETE FROM planet_osm_polygon"))
    await insert_polygon(session, 1, MOVED_SQUARE, leisure="park")
    counts = await load_green_areas(session, TEST_BBOX)

    assert counts == {GreenKind.PARK.value: 1}
    stmt = select(func.ST_X(func.ST_Centroid(GreenArea.geom)))
    assert (await session.execute(stmt)).scalars().one() == pytest.approx(128.601, abs=1e-4)


async def test_manual_row_without_osm_id_survives_reload(osm_tables: AsyncSession) -> None:
    session = osm_tables
    # 손으로 넣은 녹지(osm_id 없음)는 재적재가 지우지 않는다
    await session.execute(
        text(
            "INSERT INTO green_areas (id, kind, geom, osm_id, city) VALUES "
            "(gen_random_uuid(), :kind, ST_Multi(ST_GeomFromText(:wkt, 4326)), NULL, :city)"
        ),
        {"kind": GreenKind.PARK.value, "wkt": IN_BBOX_SQUARE, "city": TEST_BBOX.city},
    )
    await insert_polygon(session, 1, leisure="park")

    counts = await load_green_areas(session, TEST_BBOX)

    assert counts == {GreenKind.PARK.value: 2}


# --- bbox 파일 -------------------------------------------------------------


def test_unknown_city_raises_load_error() -> None:
    with pytest.raises(LoadError):
        read_bbox("atlantis")


def test_default_city_bbox_covers_curated_places() -> None:
    """data/city_bbox.csv 의 대구 범위가 큐레이션 지점을 모두 담는지 본다."""
    bbox = read_bbox("daegu")

    with PLACES_CSV.open(encoding="utf-8", newline="") as file:
        rows = [row for row in csv.DictReader(file) if row["city"] == "daegu"]

    assert rows
    for row in rows:
        lat, lng = float(row["lat"]), float(row["lng"])
        assert bbox.min_lat <= lat <= bbox.max_lat, row["name"]
        assert bbox.min_lng <= lng <= bbox.max_lng, row["name"]


def test_bbox_csv_rows_are_ordered_min_to_max() -> None:
    assert BBOX_CSV.exists()
    bbox = read_bbox("daegu")

    assert bbox.min_lng < bbox.max_lng
    assert bbox.min_lat < bbox.max_lat


# --- 실제 OSM 추출이 적재된 DB 에서만 -------------------------------------


@pytest.fixture
async def app_session() -> AsyncIterator[AsyncSession]:
    """적재가 끝난 DB(DATABASE_URL) 를 읽기만 하는 세션.

    다른 테스트는 _test DB 를 쓰지만 OSM 적재는 앱 DB 에 한다 (docs/data/osm.md).
    인수 조건은 그 DB 를 봐야 하므로 여기서만 따로 연다. SELECT 만 한다
    """
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    async with AsyncSession(engine) as session:
        yield session
    await engine.dispose()


@pytest.mark.integration
async def test_loaded_daegu_has_sincheon_line_and_gyeongsang_park(
    app_session: AsyncSession,
) -> None:
    """인수 조건: 신천 하천 선과 경상감영공원 면이 들어 있다 (docs/data/osm.md 절차 후)."""
    stmt = select(func.count()).select_from(GreenArea).where(GreenArea.city == "daegu")
    assert (await app_session.execute(stmt)).scalar_one() > 0

    # 신천 수성교 둔치 50m 안에 하천 선, 경상감영공원 좌표 50m 안에 공원 면
    assert await count_near(app_session, GreenKind.RIVER, lat=35.861371, lng=128.608886) > 0
    assert await count_near(app_session, GreenKind.PARK, lat=35.872268, lng=128.592050) > 0
