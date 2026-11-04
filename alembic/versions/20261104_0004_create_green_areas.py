"""create green areas

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-04 14:08:08.537161

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from geoalchemy2 import Geometry

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 자동 생성 결과를 확인했다. CHECK 는 면·선만 허용한다. drop 문은 없다
    op.create_geospatial_table(
        "green_areas",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column(
            "geom",
            Geometry(
                srid=4326,
                dimension=2,
                spatial_index=False,
                from_text="ST_GeomFromEWKT",
                name="geometry",
                nullable=False,
            ),
            nullable=False,
        ),
        sa.Column("osm_id", sa.BigInteger(), nullable=True),
        sa.Column("city", sa.String(length=20), nullable=False),
        sa.CheckConstraint(
            "GeometryType(geom) IN ('POLYGON', 'MULTIPOLYGON', 'LINESTRING', 'MULTILINESTRING')",
            name=op.f("ck_green_areas_geom_type"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_green_areas")),
        sa.UniqueConstraint("osm_id", "kind", name="uq_green_areas_osm_id_kind"),
    )
    op.create_geospatial_index(
        "idx_green_areas_geom",
        "green_areas",
        ["geom"],
        unique=False,
        postgresql_using="gist",
        postgresql_ops={},
    )
    op.create_index(op.f("ix_green_areas_city"), "green_areas", ["city"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_green_areas_city"), table_name="green_areas")
    op.drop_geospatial_index(
        "idx_green_areas_geom",
        table_name="green_areas",
        postgresql_using="gist",
        column_name="geom",
    )
    op.drop_geospatial_table("green_areas")
