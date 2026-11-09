"""commute routes state

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-09 22:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0020"
down_revision: str | Sequence[str] | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 샛길 생성 진행 상태 (#97). 경로가 이미 있는 동선은 ready
    op.add_column(
        "commutes",
        sa.Column("routes_state", sa.String(length=10), server_default="idle", nullable=False),
    )
    op.add_column(
        "commutes", sa.Column("routes_updated_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.execute(
        """
        update commutes c set routes_state = 'ready', routes_updated_at = now()
        where exists (select 1 from routes r where r.commute_id = c.id)
        """
    )


def downgrade() -> None:
    op.drop_column("commutes", "routes_updated_at")
    op.drop_column("commutes", "routes_state")
