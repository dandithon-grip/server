"""OSM 녹지 적재 스크립트.

    uv run python -m scripts.load_green_areas [도시코드]

osm2pgsql 이 적재한 planet_osm_polygon · planet_osm_line 에서 공원·숲·잔디·물(면)과
하천·가로수열(선)을 골라 green_areas 에 넣는다. 적재 SQL 은 scripts/sql/load_green_areas.sql,
OSM 추출부터의 전체 절차는 docs/data/osm.md 에 있다.
도시 범위(bbox)는 data/city_bbox.csv 에서 읽는다. 도시 이름을 코드에 쓰지 않는다.
(osm_id, kind) 로 upsert 하므로 여러 번 실행해도 결과가 같다.
"""

import asyncio
import csv
import sys
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import session_factory
from app.domain.route.models import GreenArea

BBOX_CSV = Path("data/city_bbox.csv")
SQL_PATH = Path("scripts/sql/load_green_areas.sql")

# natural=water 를 물가(river)로 칠 최소 면적. 공원 안 연못은 물가가 아니다
MIN_WATER_AREA_M2 = 10_000


class LoadError(Exception):
    """bbox 파일이나 적재 SQL 이 없거나 값이 잘못됐을 때."""


class CityBBox(BaseModel):
    """도시 하나의 적재 범위. osmium extract 와 적재 SQL 이 같은 값을 쓴다."""

    model_config = ConfigDict(frozen=True)

    city: str = Field(..., min_length=1, max_length=20)
    min_lng: float = Field(..., ge=-180, le=180)
    min_lat: float = Field(..., ge=-90, le=90)
    max_lng: float = Field(..., ge=-180, le=180)
    max_lat: float = Field(..., ge=-90, le=90)

    @property
    def osmium_bbox(self) -> str:
        """osmium extract --bbox 에 그대로 넣는 문자열."""
        return f"{self.min_lng},{self.min_lat},{self.max_lng},{self.max_lat}"


def read_bbox(city: str, csv_path: Path = BBOX_CSV) -> CityBBox:
    """
    도시 범위를 CSV 에서 읽는다.

    :param city: 도시 코드 (CSV 의 city 열)
    :param csv_path: 기본 data/city_bbox.csv
    :return: CityBBox
    :raises LoadError: 파일이 없거나, 해당 도시 행이 없거나, 값이 잘못됨
    """
    # 1. 파일 확인
    if not csv_path.exists():
        raise LoadError(f"{csv_path} 가 없다")

    # 2. 도시 행 찾기
    with csv_path.open(encoding="utf-8", newline="") as file:
        rows = [row for row in csv.DictReader(file) if row.get("city") == city]
    if not rows:
        raise LoadError(f"{csv_path} 에 {city} 행이 없다")

    # 3. 값 검증. min < max 까지 본다
    try:
        bbox = CityBBox.model_validate(rows[0])
    except ValidationError as error:
        fields = ", ".join(str(item["loc"][0]) for item in error.errors())
        raise LoadError(f"{csv_path} 의 {city} 행 값이 잘못됐다: {fields}") from error
    if bbox.min_lng >= bbox.max_lng or bbox.min_lat >= bbox.max_lat:
        raise LoadError(f"{csv_path} 의 {city} 범위가 뒤집혀 있다: {bbox.osmium_bbox}")
    return bbox


def read_sql(sql_path: Path = SQL_PATH) -> str:
    """
    적재 SQL 파일을 읽는다.

    :param sql_path: 기본 scripts/sql/load_green_areas.sql
    :return: SQL 문 하나
    :raises LoadError: 파일이 없음
    """
    if not sql_path.exists():
        raise LoadError(f"{sql_path} 가 없다")
    return sql_path.read_text(encoding="utf-8")


async def load_green_areas(
    session: AsyncSession, bbox: CityBBox, sql_path: Path = SQL_PATH
) -> dict[str, int]:
    """
    OSM 테이블에서 녹지를 골라 green_areas 에 upsert 한다.

    :param session: DB 세션
    :param bbox: 적재 범위와 도시 코드
    :param sql_path: 적재 SQL 파일. 기본 scripts/sql/load_green_areas.sql
    :return: kind 별 행 수 (적재 후 해당 도시 기준)
    :raises LoadError: SQL 파일이 없음
    """
    # 1. 적재 SQL 실행. 바인드로 도시와 범위만 넘긴다
    await session.execute(
        text(read_sql(sql_path)),
        {
            "city": bbox.city,
            "min_lng": bbox.min_lng,
            "min_lat": bbox.min_lat,
            "max_lng": bbox.max_lng,
            "max_lat": bbox.max_lat,
            "min_water_area_m2": MIN_WATER_AREA_M2,
        },
    )
    await session.commit()

    # 2. kind 별 행 수 집계
    stmt = (
        select(GreenArea.kind, func.count())
        .where(GreenArea.city == bbox.city)
        .group_by(GreenArea.kind)
    )
    rows = (await session.execute(stmt)).all()
    return {kind: count for kind, count in rows}


async def main(city: str) -> None:
    bbox = read_bbox(city)
    async with session_factory() as session:
        counts = await load_green_areas(session, bbox)
    total = sum(counts.values())
    detail = ", ".join(f"{kind} {count}" for kind, count in sorted(counts.items()))
    print(f"{city} 녹지 {total}개 ({detail or '없음'}) / 범위 {bbox.osmium_bbox}")


if __name__ == "__main__":
    try:
        asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else get_settings().default_city))
    except LoadError as error:
        # 스택 트레이스 대신 무엇이 틀렸는지 한 줄만 보여준다
        sys.exit(f"적재 중단: {error}")
