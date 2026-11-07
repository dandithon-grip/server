"""card llm usage

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-07 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0015"
down_revision: str | Sequence[str] | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 카드마다 LLM 입력·출력 토큰과 응답 시간 (#59). 템플릿 카드는 비어 있다
    op.add_column("today_cards", sa.Column("llm_input_tokens", sa.Integer(), nullable=True))
    op.add_column("today_cards", sa.Column("llm_output_tokens", sa.Integer(), nullable=True))
    op.add_column("today_cards", sa.Column("llm_ms", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("today_cards", "llm_ms")
    op.drop_column("today_cards", "llm_output_tokens")
    op.drop_column("today_cards", "llm_input_tokens")
