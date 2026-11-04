"""create places

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-04 13:30:04.750513

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from geoalchemy2 import Geometry
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 자동 생성 결과를 확인했다. drop 문은 없다
    op.create_geospatial_table(
        "places",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=50), nullable=False),
        sa.Column(
            "location",
            Geometry(
                geometry_type="POINT",
                srid=4326,
                dimension=2,
                spatial_index=False,
                from_text="ST_GeomFromEWKT",
                name="geometry",
                nullable=False,
            ),
            nullable=False,
        ),
        sa.Column("types", postgresql.ARRAY(sa.String(length=10)), nullable=False),
        sa.Column("description", sa.String(length=200), nullable=False),
        sa.Column("season_tag", sa.String(length=30), nullable=True),
        sa.Column("pair_id", sa.String(length=50), nullable=True),
        sa.Column("city", sa.String(length=20), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_places")),
        sa.UniqueConstraint("city", "name", name="uq_places_city_name"),
    )
    op.create_geospatial_index(
        "idx_places_location",
        "places",
        ["location"],
        unique=False,
        postgresql_using="gist",
        postgresql_ops={},
    )
    op.create_index(op.f("ix_places_city"), "places", ["city"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_places_city"), table_name="places")
    op.drop_geospatial_index(
        "idx_places_location", table_name="places", postgresql_using="gist", column_name="location"
    )
    op.drop_geospatial_table("places")
