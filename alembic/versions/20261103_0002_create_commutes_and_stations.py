"""create commutes and stations

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-03 19:04:16.971768

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from geoalchemy2 import Geometry
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Geometry 컬럼은 GIST 공간 인덱스를 따로 만든다 (create_geospatial_index).
    # 자동 생성 결과를 확인했고 drop 문은 없다
    op.create_geospatial_table(
        "stations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("line", sa.String(length=10), nullable=False),
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
        sa.Column("order_no", sa.Integer(), nullable=False),
        sa.Column("city", sa.String(length=20), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stations")),
        sa.UniqueConstraint("city", "line", "name", name="uq_stations_city_line_name"),
    )
    op.create_geospatial_index(
        "idx_stations_location",
        "stations",
        ["location"],
        unique=False,
        postgresql_using="gist",
        postgresql_ops={},
    )
    op.create_index(op.f("ix_stations_city"), "stations", ["city"], unique=False)
    op.create_geospatial_table(
        "commutes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "from_location",
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
        sa.Column(
            "to_location",
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
        sa.Column("from_area", sa.String(length=50), nullable=False),
        sa.Column("to_area", sa.String(length=50), nullable=False),
        sa.Column("go_time", sa.Time(), nullable=False),
        sa.Column("leave_time", sa.Time(), nullable=False),
        sa.Column("transport_mode", sa.String(length=10), nullable=False),
        sa.Column("line", sa.String(length=10), nullable=True),
        sa.Column("station_ids", postgresql.ARRAY(sa.Uuid()), nullable=False),
        sa.Column("preference", sa.String(length=10), nullable=False),
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
            ["user_id"], ["users.id"], name=op.f("fk_commutes_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_commutes")),
        sa.UniqueConstraint("user_id", name=op.f("uq_commutes_user_id")),
    )
    op.create_geospatial_index(
        "idx_commutes_from_location",
        "commutes",
        ["from_location"],
        unique=False,
        postgresql_using="gist",
        postgresql_ops={},
    )
    op.create_geospatial_index(
        "idx_commutes_to_location",
        "commutes",
        ["to_location"],
        unique=False,
        postgresql_using="gist",
        postgresql_ops={},
    )


def downgrade() -> None:
    op.drop_geospatial_index(
        "idx_commutes_to_location",
        table_name="commutes",
        postgresql_using="gist",
        column_name="to_location",
    )
    op.drop_geospatial_index(
        "idx_commutes_from_location",
        table_name="commutes",
        postgresql_using="gist",
        column_name="from_location",
    )
    op.drop_geospatial_table("commutes")
    op.drop_index(op.f("ix_stations_city"), table_name="stations")
    op.drop_geospatial_index(
        "idx_stations_location",
        table_name="stations",
        postgresql_using="gist",
        column_name="location",
    )
    op.drop_geospatial_table("stations")
