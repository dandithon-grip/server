"""create users

Revision ID: 0001
Revises:
Create Date: 2026-10-28

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # PostGIS 는 앞으로 올 모든 공간 테이블의 전제이므로 첫 마이그레이션에서 켠다
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")

    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("supabase_id", sa.String(length=64), nullable=False),
        sa.Column("nickname", sa.String(length=50), nullable=False),
        sa.Column("city", sa.String(length=20), nullable=False),
        sa.Column("notify_on", sa.Boolean(), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
    )
    op.create_index(op.f("ix_users_supabase_id"), "users", ["supabase_id"], unique=True)


def downgrade() -> None:
    op.drop_index(op.f("ix_users_supabase_id"), table_name="users")
    op.drop_table("users")
    # postgis 확장은 다른 테이블이 쓸 수 있으므로 내리지 않는다
