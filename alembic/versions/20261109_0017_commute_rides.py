"""commute rides

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-09 16:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0017"
down_revision: str | Sequence[str] | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 동선당 탑승 계획 ≤3 (#88). 기존 동선의 정류장은 계획 0 으로 옮긴다
    op.create_table(
        "commute_rides",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("commute_id", sa.Uuid(), nullable=False),
        sa.Column("order_no", sa.Integer(), nullable=False),
        sa.Column("line", sa.String(length=30), nullable=False),
        sa.Column("station_ids", postgresql.ARRAY(sa.Uuid()), nullable=False),
        sa.Column("alight_early_station_ids", postgresql.ARRAY(sa.Uuid()), nullable=False),
        sa.Column("board_late_station_ids", postgresql.ARRAY(sa.Uuid()), nullable=False),
        sa.Column("board_name", sa.String(length=50), nullable=False),
        sa.Column("alight_name", sa.String(length=50), nullable=False),
        sa.ForeignKeyConstraint(
            ["commute_id"],
            ["commutes.id"],
            name="fk_commute_rides_commute_id_commutes",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_commute_rides"),
        sa.UniqueConstraint("commute_id", "order_no", name="uq_commute_rides_order"),
    )
    op.create_index("ix_commute_rides_commute_id", "commute_rides", ["commute_id"])
    op.add_column("routes", sa.Column("ride_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_routes_ride_id_commute_rides",
        "routes",
        "commute_rides",
        ["ride_id"],
        ["id"],
        ondelete="SET NULL",
    )
    # 기존 동선 → 계획 0. 승차·하차 이름은 정류장 행에서 가져온다
    op.execute(
        """
        insert into commute_rides
            (id, commute_id, order_no, line, station_ids, alight_early_station_ids,
             board_late_station_ids, board_name, alight_name)
        select gen_random_uuid(), c.id, 0, coalesce(c.line, ''), c.station_ids,
               c.alight_early_station_ids, c.board_late_station_ids,
               coalesce((select s.name from stations s where s.id = c.station_ids[1]), ''),
               coalesce((select s.name from stations s
                         where s.id = c.station_ids[cardinality(c.station_ids)]), '')
        from commutes c
        where c.transport_mode = 'transit' and cardinality(c.station_ids) > 0
        """
    )
    op.execute(
        """
        update routes r set ride_id = cr.id
        from commute_rides cr
        where cr.commute_id = r.commute_id and cr.order_no = 0
          and r.leg_type in ('front_walk', 'back_walk', 'alight_early', 'board_late')
        """
    )


def downgrade() -> None:
    op.drop_constraint("fk_routes_ride_id_commute_rides", "routes", type_="foreignkey")
    op.drop_column("routes", "ride_id")
    op.drop_index("ix_commute_rides_commute_id", table_name="commute_rides")
    op.drop_table("commute_rides")
