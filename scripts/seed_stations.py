"""정류장 시드 스크립트.

    uv run python -m scripts.seed_stations [CSV 경로]

CSV 헤더: city,line,order_no,name,lat,lng
(city, line, name) 이 같으면 좌표와 순서를 갱신한다. 여러 번 실행해도 결과가 같다.
기본 파일은 data/stations_daegu_line2.csv (데모 2호선).
"""

import asyncio
import csv
import sys
from pathlib import Path

from geoalchemy2.shape import from_shape
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from shapely.geometry import Point
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import session_factory
from app.domain.commute.models import LINE_MAX_LENGTH, STATION_NAME_MAX_LENGTH, Station

DEFAULT_CSV = Path("data/stations_daegu_line2.csv")


class SeedError(Exception):
    """CSV 내용이 잘못됐을 때. 메시지에 행 번호가 들어간다."""


class StationRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    city: str = Field(..., min_length=1, max_length=20)
    line: str = Field(..., min_length=1, max_length=LINE_MAX_LENGTH)
    order_no: int = Field(..., ge=0)
    name: str = Field(..., min_length=1, max_length=STATION_NAME_MAX_LENGTH)
    lat: float = Field(..., ge=-90, le=90)
    lng: float = Field(..., ge=-180, le=180)


def read_rows(csv_path: Path) -> list[StationRow]:
    """CSV 를 읽어 검증한다. 틀린 행은 번호(헤더가 1행)와 함께 SeedError 로 알린다."""
    with csv_path.open(encoding="utf-8", newline="") as file:
        rows: list[StationRow] = []
        for line_no, raw in enumerate(csv.DictReader(file), start=2):
            try:
                rows.append(StationRow.model_validate(raw))
            except ValidationError as error:
                fields = ", ".join(str(item["loc"][0]) for item in error.errors())
                raise SeedError(
                    f"{csv_path} {line_no}행 값이 비었거나 잘못됐다: {fields}"
                ) from error
    return rows


async def seed_stations(session: AsyncSession, csv_path: Path) -> int:
    """
    CSV 의 정류장을 upsert 한다.

    :return: 처리한 행 수
    :raises SeedError: CSV 값이 잘못됨
    """
    # 1. 읽고 검증
    rows = read_rows(csv_path)
    if not rows:
        return 0

    # 2. (city, line, name) 충돌이면 좌표·순서 갱신
    values = [
        {
            "city": row.city,
            "line": row.line,
            "order_no": row.order_no,
            "name": row.name,
            "location": from_shape(Point(row.lng, row.lat), srid=4326),
        }
        for row in rows
    ]
    stmt = insert(Station).values(values)
    # 시드 정류장은 stationID 가 없다. (도시, 노선, 이름) 부분 unique 인덱스로 upsert 한다
    stmt = stmt.on_conflict_do_update(
        index_elements=["city", "line", "name"],
        index_where=Station.external_id.is_(None),
        set_={"order_no": stmt.excluded.order_no, "location": stmt.excluded.location},
    )
    await session.execute(stmt)
    await session.commit()
    return len(rows)


async def main(csv_path: Path) -> None:
    async with session_factory() as session:
        count = await seed_stations(session, csv_path)
    print(f"정류장 {count}개 처리 ({csv_path})")


if __name__ == "__main__":
    try:
        asyncio.run(main(Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CSV))
    except SeedError as error:
        # 스택 트레이스 대신 무엇이 틀렸는지 한 줄만 보여준다
        sys.exit(f"시드 중단: {error}")
