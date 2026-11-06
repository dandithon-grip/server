"""route direction

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-06 22:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0014"
down_revision: str | Sequence[str] | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 출근·퇴근 방향 (#56). 기존 경로는 모두 출근 방향으로 만들어졌다
    op.add_column(
        "routes",
        sa.Column("direction", sa.String(length=10), server_default="to_work", nullable=False),
    )
    op.add_column(
        "routes",
        sa.Column("sunset_ratio_reverse", sa.Float(), server_default="0", nullable=False),
    )
    op.add_column("routes", sa.Column("name_reverse", sa.String(length=60), nullable=True))


def downgrade() -> None:
    op.drop_column("routes", "name_reverse")
    op.drop_column("routes", "sunset_ratio_reverse")
    op.drop_column("routes", "direction")
