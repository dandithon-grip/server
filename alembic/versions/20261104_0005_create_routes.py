"""create routes

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-04 15:50:27.796339

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from geoalchemy2 import Geometry
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 자동 생성 결과를 확인했다. drop 문은 없다
    op.create_geospatial_table(
        "routes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("commute_id", sa.Uuid(), nullable=False),
        sa.Column("leg_type", sa.String(length=20), nullable=False),
        sa.Column("is_baseline", sa.Boolean(), nullable=False),
        sa.Column("name", sa.String(length=60), nullable=False),
        sa.Column("minutes", sa.Float(), nullable=False),
        sa.Column("add_minutes", sa.Float(), nullable=False),
        sa.Column(
            "polyline",
            Geometry(
                geometry_type="LINESTRING",
                srid=4326,
                dimension=2,
                spatial_index=False,
                from_text="ST_GeomFromEWKT",
                name="geometry",
                nullable=False,
            ),
            nullable=False,
        ),
        sa.Column("green_pct", sa.Float(), nullable=False),
        sa.Column("water_score", sa.Float(), nullable=False),
        sa.Column("landmark_score", sa.Float(), nullable=False),
        sa.Column("sunset_ratio", sa.Float(), nullable=False),
        sa.Column("tags", postgresql.ARRAY(sa.String(length=10)), nullable=False),
        sa.Column("spots", postgresql.ARRAY(sa.Uuid()), nullable=False),
        sa.Column("highlights", postgresql.ARRAY(sa.String(length=200)), nullable=False),
        sa.Column("alight_station_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["alight_station_id"],
            ["stations.id"],
            name=op.f("fk_routes_alight_station_id_stations"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["commute_id"],
            ["commutes.id"],
            name=op.f("fk_routes_commute_id_commutes"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_routes")),
    )
    op.create_geospatial_index(
        "idx_routes_polyline",
        "routes",
        ["polyline"],
        unique=False,
        postgresql_using="gist",
        postgresql_ops={},
    )
    op.create_index(op.f("ix_routes_commute_id"), "routes", ["commute_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_routes_commute_id"), table_name="routes")
    op.drop_geospatial_index(
        "idx_routes_polyline", table_name="routes", postgresql_using="gist", column_name="polyline"
    )
    op.drop_geospatial_table("routes")
