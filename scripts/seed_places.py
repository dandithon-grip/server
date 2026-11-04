"""큐레이션 자연 지점 시드 스크립트.

    uv run python -m scripts.seed_places [CSV 경로]

CSV 헤더: city,name,lat,lng,types,description,season_tag,pair_id
- types: PlaceType 값을 | 로 구분 (예: river|sunset)
- season_tag, pair_id: 비워도 된다
- pair_id 가 있으면 같은 값이 정확히 2행이어야 한다 (하천 진입·진출 쌍)
(city, name) 이 같으면 나머지 값을 갱신한다. 여러 번 실행해도 결과가 같다.
기본 파일은 data/places_daegu.csv.
"""

import asyncio
import csv
import sys
from collections import Counter
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import session_factory
from app.core.geo import make_point
from app.domain.route.models import (
    DESCRIPTION_MAX_LENGTH,
    PAIR_ID_MAX_LENGTH,
    PLACE_NAME_MAX_LENGTH,
    SEASON_TAG_MAX_LENGTH,
    Place,
    PlaceType,
)

DEFAULT_CSV = Path("data/places_daegu.csv")
TYPE_SEPARATOR = "|"


class SeedError(Exception):
    """CSV 내용이 잘못됐을 때. 메시지에 행 번호나 pair_id 가 들어간다."""


class PlaceRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    city: str = Field(..., min_length=1, max_length=20)
    name: str = Field(..., min_length=1, max_length=PLACE_NAME_MAX_LENGTH)
    lat: float = Field(..., ge=-90, le=90)
    lng: float = Field(..., ge=-180, le=180)
    types: list[PlaceType] = Field(..., min_length=1)
    description: str = Field(..., min_length=1, max_length=DESCRIPTION_MAX_LENGTH)
    season_tag: str | None = Field(None, max_length=SEASON_TAG_MAX_LENGTH)
    pair_id: str | None = Field(None, max_length=PAIR_ID_MAX_LENGTH)

    @field_validator("types", mode="before")
    @classmethod
    def split_types(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip() for part in value.split(TYPE_SEPARATOR) if part.strip()]
        return value

    @field_validator("season_tag", "pair_id", mode="before")
    @classmethod
    def blank_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value


def read_rows(csv_path: Path) -> list[PlaceRow]:
    """CSV 를 읽어 행 단위·쌍 단위로 검증한다. 틀리면 SeedError."""
    # 1. 행 검증 (헤더가 1행)
    with csv_path.open(encoding="utf-8", newline="") as file:
        rows: list[PlaceRow] = []
        for line_no, raw in enumerate(csv.DictReader(file), start=2):
            try:
                rows.append(PlaceRow.model_validate(raw))
            except ValidationError as error:
                fields = ", ".join(str(item["loc"][0]) for item in error.errors())
                raise SeedError(
                    f"{csv_path} {line_no}행 값이 비었거나 잘못됐다: {fields}"
                ) from error

    # 2. 쌍 검증. pair_id 하나에 정확히 2행
    counts = Counter(row.pair_id for row in rows if row.pair_id is not None)
    broken = sorted(pair_id for pair_id, count in counts.items() if count != 2)
    if broken:
        raise SeedError(f"{csv_path} pair_id 가 2행이 아니다: {', '.join(broken)}")
    return rows


async def seed_places(session: AsyncSession, csv_path: Path) -> int:
    """
    CSV 의 지점을 upsert 한다.

    :return: 처리한 행 수
    :raises SeedError: CSV 값이 잘못됨
    """
    # 1. 읽고 검증
    rows = read_rows(csv_path)
    if not rows:
        return 0

    # 2. (city, name) 충돌이면 나머지 값 갱신
    values = [
        {
            "city": row.city,
            "name": row.name,
            "location": make_point(row.lat, row.lng),
            "types": [place_type.value for place_type in row.types],
            "description": row.description,
            "season_tag": row.season_tag,
            "pair_id": row.pair_id,
        }
        for row in rows
    ]
    stmt = insert(Place).values(values)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_places_city_name",
        set_={
            "location": stmt.excluded.location,
            "types": stmt.excluded.types,
            "description": stmt.excluded.description,
            "season_tag": stmt.excluded.season_tag,
            "pair_id": stmt.excluded.pair_id,
        },
    )
    await session.execute(stmt)
    await session.commit()
    return len(rows)


async def main(csv_path: Path) -> None:
    async with session_factory() as session:
        count = await seed_places(session, csv_path)
    print(f"자연 지점 {count}개 처리 ({csv_path})")


if __name__ == "__main__":
    try:
        asyncio.run(main(Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CSV))
    except SeedError as error:
        # 스택 트레이스 대신 무엇이 틀렸는지 한 줄만 보여준다
        sys.exit(f"시드 중단: {error}")
