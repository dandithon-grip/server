"""자연 지점 시드 기대 동작.

CSV 를 읽어 (city, name) 기준으로 upsert 하고, 태그·쌍·좌표를 검증한다.
"""

from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.route.models import Place, PlaceType
from scripts.seed_places import SeedError, seed_places

HEADER = "city,name,lat,lng,types,description,season_tag,pair_id\n"
ROWS = (
    "daegu,신천 수성교 진입,35.8560,128.6090,river|trees,둔치로 내려가는 길,억새,sincheon-a\n"
    "daegu,신천 상동교 진출,35.8470,128.6110,river|sunset|bridge,서쪽으로 해가 진다,,sincheon-a\n"
    "daegu,경상감영공원,35.8720,128.5930,park|trees,느티나무 그늘,,\n"
)
BAD_TYPE_ROW = "daegu,경상감영공원,35.8720,128.5930,forest,설명,,\n"


def write_csv(path: Path, rows: str) -> Path:
    path.write_text(HEADER + rows, encoding="utf-8")
    return path


async def count_places(session: AsyncSession) -> int:
    return (await session.execute(select(func.count()).select_from(Place))).scalar_one()


async def test_seed_inserts_rows_with_types_and_pair(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    upserted = await seed_places(db_session, write_csv(tmp_path / "places.csv", ROWS))

    assert upserted == 3
    assert await count_places(db_session) == 3
    stmt = select(Place).where(Place.name == "신천 상동교 진출")
    place = (await db_session.execute(stmt)).scalar_one()
    assert place.place_types == [PlaceType.RIVER, PlaceType.SUNSET, PlaceType.BRIDGE]
    assert place.pair_id == "sincheon-a"
    assert place.season_tag is None
    assert (place.lat, place.lng) == (35.8470, 128.6110)


async def test_seed_twice_is_idempotent_and_updates_values(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    first = write_csv(tmp_path / "v1.csv", "daegu,경상감영공원,35.0,128.0,park,옛 설명,,\n")
    second = write_csv(
        tmp_path / "v2.csv", "daegu,경상감영공원,35.8720,128.5930,park|trees,새 설명,단풍,\n"
    )

    await seed_places(db_session, first)
    await seed_places(db_session, second)

    assert await count_places(db_session) == 1
    place = (await db_session.execute(select(Place))).scalar_one()
    assert place.description == "새 설명"
    assert place.season_tag == "단풍"
    assert place.place_types == [PlaceType.PARK, PlaceType.TREES]
    assert place.lat == 35.8720


async def test_unknown_type_fails_with_row_number(db_session: AsyncSession, tmp_path: Path) -> None:
    csv_path = write_csv(
        tmp_path / "bad.csv", "daegu,경상감영공원,35.8720,128.5930,forest,설명,,\n"
    )

    with pytest.raises(SeedError, match="2행"):
        await seed_places(db_session, csv_path)
    assert await count_places(db_session) == 0


async def test_unpaired_pair_id_fails(db_session: AsyncSession, tmp_path: Path) -> None:
    csv_path = write_csv(
        tmp_path / "pair.csv", "daegu,신천 수성교 진입,35.8560,128.6090,river,설명,,sincheon-a\n"
    )

    with pytest.raises(SeedError, match="sincheon-a"):
        await seed_places(db_session, csv_path)


async def test_blank_coordinate_fails_with_row_number(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    csv_path = write_csv(tmp_path / "blank.csv", "daegu,경상감영공원,,,park,설명,,\n")

    with pytest.raises(SeedError, match="2행"):
        await seed_places(db_session, csv_path)
