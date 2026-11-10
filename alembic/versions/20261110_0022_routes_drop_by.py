"""routes drop_by

Revision ID: 0022
Revises: 0021
Create Date: 2026-11-10 15:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0022"
down_revision: str | Sequence[str] | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 후보가 없을 때 가까운 자연 1곳에 들렀다 오는 왕복 경로 표시 (#116). 기존 행은 false
    op.add_column(
        "routes",
        sa.Column("drop_by", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_column("routes", "drop_by")
