"""commute ride stop ids

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-09 20:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0019"
down_revision: str | Sequence[str] | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 저장한 경로의 정류장 순서 (#93). 기존 계획은 NULL (노선 순번으로 채움). 다시 저장하면 채워진다
    op.add_column(
        "commute_rides",
        sa.Column("stop_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("commute_rides", "stop_ids")
