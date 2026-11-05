"""create today cards and user is_demo

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-05 14:58:46.835141

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 자동 생성 결과를 확인했다. drop 문은 없다.
    # users.is_demo 는 server_default false 로 기존 행을 채운다
    op.create_table(
        "today_cards",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("route_id", sa.Uuid(), nullable=True),
        sa.Column("action_type", sa.String(length=20), nullable=False),
        sa.Column("fallback_level", sa.Integer(), nullable=False),
        sa.Column("reason_tag", sa.String(length=10), nullable=False),
        sa.Column("text", sa.String(length=120), nullable=False),
        sa.Column("deviation", sa.String(length=200), nullable=True),
        sa.Column("preference", sa.String(length=10), nullable=False),
        sa.Column("sunset_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sky", sa.String(length=10), nullable=True),
        sa.Column("air_grade", sa.String(length=10), nullable=True),
        sa.Column("by_llm", sa.Boolean(), nullable=False),
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
            ["route_id"],
            ["routes.id"],
            name=op.f("fk_today_cards_route_id_routes"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_today_cards_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_today_cards")),
        sa.UniqueConstraint("user_id", "date", name="uq_today_cards_user_id_date"),
    )
    op.create_index(op.f("ix_today_cards_user_id"), "today_cards", ["user_id"], unique=False)
    op.add_column(
        "users", sa.Column("is_demo", sa.Boolean(), server_default=sa.text("false"), nullable=False)
    )


def downgrade() -> None:
    op.drop_column("users", "is_demo")
    op.drop_index(op.f("ix_today_cards_user_id"), table_name="today_cards")
    op.drop_table("today_cards")
