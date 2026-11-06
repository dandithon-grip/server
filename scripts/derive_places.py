"""OSM 녹지에서 자동 Place(경유지) 추출.

    uv run python -m scripts.derive_places [도시코드]

green_areas 의 이름 있는 공원·숲·하천을 Place 로 만든다. 큐레이션 30곳만으로는 신천·도심 밖 동선에
돌아갈 곳이 없어서다 (명세: 자연 지점 테이블 200~300개).
- 공원·숲 (MIN_AREA_M2 이상): 대표점 1개 → park / trees
- 큰 공원 (BIG_AREA_M2 이상): 경계선을 ENTRANCE_STEP_M 간격으로 잘라 "{이름} 입구 N" 추가
- 하천 선: RIVER_STEP_M 간격 점을 연속 쌍(pair_id)으로 → 진입·진출
- 큐레이션 Place 와 DEDUPE_M 안이면 건너뛴다. (city, name) upsert 로 여러 번 실행해도 같다
선행: docs/data/osm.md 의 적재(load_green_areas), seed_places.
"""

import asyncio
import sys
from dataclasses import dataclass

from geoalchemy2 import Geography
from sqlalchemy import cast, delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import session_factory
from app.core.geo import make_point
from app.domain.route.models import GreenArea, GreenKind, Place, PlaceType
from app.domain.route.scoring import GEOGRAPHY

MIN_AREA_M2 = 3_000  # 0.3ha. 이보다 작은 공원은 경유할 가치가 없다
BIG_AREA_M2 = 50_000  # 5ha. 이보다 크면 중심점만으론 멀어서 경계 입구를 더 찍는다
ENTRANCE_STEP_M = 500
MAX_ENTRANCES = (
    8  # 산 하나 둘레가 수십 km 라 입구가 수백 개 찍히는 것을 막는다 (실측: 상대곡숲 190개)
)
RIVER_STEP_M = 800
MIN_RIVER_LENGTH_M = 1_000  # 짧은 소하천 조각은 둔치가 없다
MAX_RIVER_POINTS_PER_NAME = 40
DEDUPE_M = 150
SOURCE_OSM = "osm"
SOURCE_CURATED = "curated"

KIND_TYPES = {
    GreenKind.PARK: [PlaceType.PARK],
    GreenKind.FOREST: [PlaceType.TREES, PlaceType.PARK],
}
DESCRIPTIONS = {
    GreenKind.PARK: "{name}을 가로지르는 길",
    GreenKind.FOREST: "{name} 숲길",
}


@dataclass(frozen=True)
class Candidate:
    name: str
    lat: float
    lng: float
    types: list[PlaceType]
    description: str
    pair_id: str | None = None


@dataclass(frozen=True)
class DeriveResult:
    parks: int
    entrances: int
    river_points: int
    skipped_near_curated: int
    upserted: int


async def derive_places(session: AsyncSession, city: str) -> DeriveResult:
    """
    도시의 green_areas 에서 자동 Place 를 만들어 upsert 한다.

    :return: 종류별 생성 수와 큐레이션 근처라 건너뛴 수
    """
    # 1. 후보 수집
    parks, entrances = await _park_candidates(session, city)
    rivers = await _river_candidates(session, city)
    candidates = _unique_names(parks + entrances + rivers)

    # 2. 큐레이션 150m 안은 제외
    curated = await _curated_points(session, city)
    kept = [c for c in candidates if not _near_any(c, curated)]
    skipped = len(candidates) - len(kept)
    if not kept:
        return DeriveResult(len(parks), len(entrances), len(rivers), skipped, 0)

    # 3. 자동 행은 전부 지우고 다시 넣는다 (조건값이 바뀌면 옛 행이 남지 않게).
    #    큐레이션 행은 건드리지 않는다
    await session.execute(delete(Place).where(Place.city == city, Place.source == SOURCE_OSM))
    values = [
        {
            "name": c.name,
            "location": make_point(c.lat, c.lng),
            "types": [t.value for t in c.types],
            "description": c.description,
            "season_tag": None,
            "pair_id": c.pair_id,
            "city": city,
            "source": SOURCE_OSM,
        }
        for c in kept
    ]
    stmt = insert(Place).values(values)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_places_city_name",
        set_={
            "location": stmt.excluded.location,
            "types": stmt.excluded.types,
            "description": stmt.excluded.description,
            "pair_id": stmt.excluded.pair_id,
        },
        where=(Place.source == SOURCE_OSM),
    )
    await session.execute(stmt)
    await session.commit()
    return DeriveResult(len(parks), len(entrances), len(rivers), skipped, len(kept))


async def _park_candidates(
    session: AsyncSession, city: str
) -> tuple[list[Candidate], list[Candidate]]:
    """이름 있는 공원·숲의 대표점과, 큰 공원의 경계 입구."""
    geog = cast(GreenArea.geom, Geography)
    point = func.ST_PointOnSurface(GreenArea.geom)
    stmt = (
        select(
            GreenArea.name,
            GreenArea.kind,
            func.ST_Area(geog).label("area"),
            func.ST_Y(point).label("lat"),
            func.ST_X(point).label("lng"),
            GreenArea.geom,
        )
        .where(
            GreenArea.city == city,
            GreenArea.name.is_not(None),
            GreenArea.kind.in_([GreenKind.PARK.value, GreenKind.FOREST.value]),
            func.ST_Area(geog) >= MIN_AREA_M2,
        )
        .order_by(func.ST_Area(geog).desc())
    )
    parks: list[Candidate] = []
    entrances: list[Candidate] = []
    for name, kind_value, area, lat, lng, geom in (await session.execute(stmt)).all():
        kind = GreenKind(kind_value)
        parks.append(
            Candidate(
                name=name,
                lat=float(lat),
                lng=float(lng),
                types=KIND_TYPES[kind],
                description=DESCRIPTIONS[kind].format(name=name),
            )
        )
        if area >= BIG_AREA_M2:
            entrances.extend(await _boundary_points(session, name, kind, geom))
    return parks, entrances


async def _boundary_points(
    session: AsyncSession, name: str, kind: GreenKind, geom: object
) -> list[Candidate]:
    """가장 큰 폴리곤의 바깥 경계선을 ENTRANCE_STEP_M 간격으로 잘라 입구 후보로."""
    ring = func.ST_ExteriorRing(func.ST_GeometryN(geom, 1))
    length = (await session.execute(select(func.ST_Length(cast(ring, GEOGRAPHY))))).scalar_one()
    center = func.ST_Centroid(geom)
    c_lat, c_lng = (await session.execute(select(func.ST_Y(center), func.ST_X(center)))).one()
    count = min(max(int(length // ENTRANCE_STEP_M), 2), MAX_ENTRANCES)
    points: list[Candidate] = []
    for index in range(count):
        fraction = index / count
        point = func.ST_LineInterpolatePoint(ring, fraction)
        lat, lng = (await session.execute(select(func.ST_Y(point), func.ST_X(point)))).one()
        side = compass(float(c_lat), float(c_lng), float(lat), float(lng))
        points.append(
            Candidate(
                name=f"{name} {side}쪽 입구",
                lat=float(lat),
                lng=float(lng),
                types=KIND_TYPES[kind],
                description=f"{name} {side}쪽 가장자리를 따라 걷는 길",
            )
        )
    return points


COMPASS = ["북", "북동", "동", "남동", "남", "남서", "서", "북서"]


def compass(from_lat: float, from_lng: float, to_lat: float, to_lng: float) -> str:
    """중심에서 본 방향 8방위. 입구 이름에 쓴다 ("두류공원 북쪽 입구")."""
    from app.clients.tmap import LatLng
    from app.domain.route.scoring import heading_deg

    bearing = heading_deg(LatLng(lat=from_lat, lng=from_lng), LatLng(lat=to_lat, lng=to_lng))
    return COMPASS[int(((bearing + 22.5) % 360) // 45)]


async def _river_candidates(session: AsyncSession, city: str) -> list[Candidate]:
    """이름 있는 하천 선을 RIVER_STEP_M 간격으로 잘라 연속한 두 점을 진입·진출 쌍으로."""
    merged = func.ST_LineMerge(GreenArea.geom)
    stmt = (
        select(GreenArea.name, GreenArea.osm_id, merged.label("line"))
        .where(
            GreenArea.city == city,
            GreenArea.name.is_not(None),
            GreenArea.kind == GreenKind.RIVER.value,
            func.GeometryType(GreenArea.geom).in_(["LINESTRING", "MULTILINESTRING"]),
        )
        .order_by(GreenArea.name, GreenArea.osm_id)
    )
    candidates: list[Candidate] = []
    serial: dict[str, int] = {}
    for name, osm_id, line in (await session.execute(stmt)).all():
        # 병합 결과가 MULTILINESTRING 이면 가장 긴 조각만 쓴다
        part = func.ST_GeometryN(line, 1)
        length = (await session.execute(select(func.ST_Length(cast(part, GEOGRAPHY))))).scalar_one()
        if length < MIN_RIVER_LENGTH_M or serial.get(name, 0) >= MAX_RIVER_POINTS_PER_NAME:
            continue
        count = int(length // RIVER_STEP_M) + 1
        points: list[tuple[float, float]] = []
        for index in range(count):
            fraction = min(index * RIVER_STEP_M / length, 1.0)
            point = func.ST_LineInterpolatePoint(part, fraction)
            lat, lng = (await session.execute(select(func.ST_Y(point), func.ST_X(point)))).one()
            points.append((float(lat), float(lng)))
        for a, b in zip(points, points[1:], strict=False):
            serial[name] = serial.get(name, 0) + 1
            pair_id = f"{name}-{osm_id}-{serial[name]}"
            candidates.append(
                Candidate(
                    name=f"{name} 둔치 {serial[name]} 진입",
                    lat=a[0],
                    lng=a[1],
                    types=[PlaceType.RIVER],
                    description=f"{name} 둔치로 내려가 물소리를 따라 걷는 길",
                    pair_id=pair_id,
                )
            )
            candidates.append(
                Candidate(
                    name=f"{name} 둔치 {serial[name]} 진출",
                    lat=b[0],
                    lng=b[1],
                    types=[PlaceType.RIVER],
                    description=f"{name} 둔치에서 올라오는 길",
                    pair_id=pair_id,
                )
            )
    return candidates


async def _curated_points(session: AsyncSession, city: str) -> list[tuple[float, float]]:
    stmt = select(func.ST_Y(Place.location), func.ST_X(Place.location)).where(
        Place.city == city, Place.source == SOURCE_CURATED
    )
    return [(float(lat), float(lng)) for lat, lng in (await session.execute(stmt)).all()]


def _near_any(candidate: Candidate, points: list[tuple[float, float]]) -> bool:
    from app.clients.tmap import LatLng
    from app.domain.route.service import distance_m

    here = LatLng(lat=candidate.lat, lng=candidate.lng)
    return any(distance_m(here, LatLng(lat=lat, lng=lng)) <= DEDUPE_M for lat, lng in points)


def _unique_names(candidates: list[Candidate]) -> list[Candidate]:
    """같은 이름(어린이공원 같은 흔한 이름)은 뒤에 번호를 붙인다. (city, name) 유니크 때문."""
    seen: dict[str, int] = {}
    result: list[Candidate] = []
    for c in candidates:
        seen[c.name] = seen.get(c.name, 0) + 1
        # 같은 공원의 같은 방향 입구가 둘이면 "북쪽 입구 2" 처럼 번호
        name = c.name if seen[c.name] == 1 else f"{c.name} {seen[c.name]}"
        result.append(Candidate(name[:50], c.lat, c.lng, c.types, c.description, c.pair_id))
    return result


async def main(city: str) -> None:
    async with session_factory() as session:
        result = await derive_places(session, city)
    print(
        f"{city} 자동 Place: 공원·숲 {result.parks}, 입구 {result.entrances}, "
        f"하천 점 {result.river_points}, 큐레이션 근처 제외 {result.skipped_near_curated}, "
        f"upsert {result.upserted}"
    )


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else get_settings().default_city))
