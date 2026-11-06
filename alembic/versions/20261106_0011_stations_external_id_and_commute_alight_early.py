"""stations external id and commute alight early

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-06 15:26:36.519857

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ODsay 정류장은 stationID 로 구분한다 (#44).
    # 길 건너편 같은 이름 정류장이 한 행으로 합쳐지던 문제.
    # 옛 unique (city, line, name) 는 stationID 없는 시드 정류장에만 남긴다 (부분 unique 인덱스).
    # 자동 생성이 넣은 planet_osm_* (osm2pgsql 원본) drop 문은 지웠다
    op.add_column("stations", sa.Column("external_id", sa.String(length=20), nullable=True))
    op.drop_constraint("uq_stations_city_line_name", "stations", type_="unique")
    op.create_unique_constraint(
        "uq_stations_city_line_external", "stations", ["city", "line", "external_id"]
    )
    op.create_index(
        "uq_stations_seed_city_line_name",
        "stations",
        ["city", "line", "name"],
        unique=True,
        postgresql_where=sa.text("external_id IS NULL"),
    )

    # 한 정거장 먼저 내릴 정류장을 저장 시점에 정해 둔다.
    # 다른 사용자의 저장이 순번을 바꿔도 그대로다
    op.add_column("commutes", sa.Column("alight_early_station_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_commutes_alight_early_station_id_stations",
        "commutes",
        "stations",
        ["alight_early_station_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_commutes_alight_early_station_id_stations", "commutes", type_="foreignkey"
    )
    op.drop_column("commutes", "alight_early_station_id")
    op.drop_index("uq_stations_seed_city_line_name", table_name="stations")
    op.drop_constraint("uq_stations_city_line_external", "stations", type_="unique")
    op.drop_column("stations", "external_id")
    op.create_unique_constraint("uq_stations_city_line_name", "stations", ["city", "line", "name"])
