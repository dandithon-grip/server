"""widen line columns for odsay names

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-05 18:43:50.453161

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 노선 이름을 ODsay 값("대구 2호선", "급행1")으로 저장하기 위해 10 → 30. drop 문은 없다
    op.alter_column(
        "commutes",
        "line",
        existing_type=sa.VARCHAR(length=10),
        type_=sa.String(length=30),
        existing_nullable=True,
    )
    op.alter_column(
        "stations",
        "line",
        existing_type=sa.VARCHAR(length=10),
        type_=sa.String(length=30),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "stations",
        "line",
        existing_type=sa.String(length=30),
        type_=sa.VARCHAR(length=10),
        existing_nullable=False,
    )
    op.alter_column(
        "commutes",
        "line",
        existing_type=sa.String(length=30),
        type_=sa.VARCHAR(length=10),
        existing_nullable=True,
    )
