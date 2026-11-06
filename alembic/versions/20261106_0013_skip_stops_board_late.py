"""skip stops and board late

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-06 20:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0013"
down_revision: str | Sequence[str] | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1~3 정거장 먼저 내리기·늦게 타기 (#51). 정류장 하나 → 목록
    for column in ("alight_early_station_ids", "board_late_station_ids"):
        op.add_column(
            "commutes",
            sa.Column(column, postgresql.ARRAY(sa.Uuid()), server_default="{}", nullable=False),
        )
    op.execute(
        "UPDATE commutes SET alight_early_station_ids = ARRAY[alight_early_station_id] "
        "WHERE alight_early_station_id IS NOT NULL"
    )
    # 목록으로 옮긴 한 정거장 컬럼은 지운다 (자동 생성 drop 이 아니라 의도한 drop)
    op.drop_constraint(
        "fk_commutes_alight_early_station_id_stations", "commutes", type_="foreignkey"
    )
    op.drop_column("commutes", "alight_early_station_id")

    op.add_column("routes", sa.Column("board_station_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_routes_board_station_id_stations",
        "routes",
        "stations",
        ["board_station_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column(
        "routes",
        sa.Column("skip_stops", sa.SmallInteger(), server_default="0", nullable=False),
    )
    op.execute("UPDATE routes SET skip_stops = 1 WHERE leg_type = 'alight_early'")


def downgrade() -> None:
    op.drop_column("routes", "skip_stops")
    op.drop_constraint("fk_routes_board_station_id_stations", "routes", type_="foreignkey")
    op.drop_column("routes", "board_station_id")
    op.add_column("commutes", sa.Column("alight_early_station_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_commutes_alight_early_station_id_stations",
        "commutes",
        "stations",
        ["alight_early_station_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.execute("UPDATE commutes SET alight_early_station_id = alight_early_station_ids[1]")
    op.drop_column("commutes", "board_late_station_ids")
    op.drop_column("commutes", "alight_early_station_ids")
