"""정류장 시드 기대 동작.

CSV 를 읽어 (city, line, name) 기준으로 upsert 하고, 여러 번 돌려도 결과가 같다.
"""

from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.commute.models import Station
from scripts.seed_stations import SeedError, seed_stations

HEADER = "city,line,order_no,name,lat,lng\n"


def write_csv(path: Path, rows: str) -> Path:
    path.write_text(HEADER + rows, encoding="utf-8")
    return path


async def count_stations(session: AsyncSession) -> int:
    return (await session.execute(select(func.count()).select_from(Station))).scalar_one()


async def test_seed_inserts_rows(db_session: AsyncSession, tmp_path: Path) -> None:
    csv_path = write_csv(
        tmp_path / "stations.csv",
        "daegu,2,0,반월당,35.8658,128.5933\ndaegu,2,1,경대병원,35.8623,128.6035\n",
    )

    upserted = await seed_stations(db_session, csv_path)

    assert upserted == 2
    assert await count_stations(db_session) == 2


async def test_seed_twice_is_idempotent_and_updates_coordinates(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    first = write_csv(tmp_path / "v1.csv", "daegu,2,0,반월당,35.0,128.0\n")
    second = write_csv(tmp_path / "v2.csv", "daegu,2,0,반월당,35.8658,128.5933\n")

    await seed_stations(db_session, first)
    await seed_stations(db_session, second)

    assert await count_stations(db_session) == 1
    stmt = select(func.ST_Y(Station.location)).where(Station.name == "반월당")
    assert (await db_session.execute(stmt)).scalar_one() == 35.8658


async def test_blank_coordinate_fails_with_row_number(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    csv_path = write_csv(tmp_path / "bad.csv", "daegu,2,0,반월당,,\n")

    with pytest.raises(SeedError, match="2행"):
        await seed_stations(db_session, csv_path)
