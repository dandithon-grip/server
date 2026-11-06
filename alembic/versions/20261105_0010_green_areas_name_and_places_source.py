"""green_areas name and places source

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-05 22:50:09.323640

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # green_areas.name (OSM 이름, 자동 Place 후보), places.source (curated/osm). drop 문은 없다
    op.add_column("green_areas", sa.Column("name", sa.String(length=100), nullable=True))
    op.add_column(
        "places",
        sa.Column(
            "source", sa.String(length=10), server_default=sa.text("'curated'"), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_column("places", "source")
    op.drop_column("green_areas", "name")
