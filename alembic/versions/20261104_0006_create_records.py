"""create records

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-04 18:00:33.124032

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 자동 생성 결과를 확인했다. drop 문(#9 가 적재한 planet_osm_* 테이블)은 지웠다
    op.create_table(
        "records",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("place_id", sa.Uuid(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("season", sa.String(length=10), nullable=False),
        sa.Column("stage", sa.String(length=10), nullable=False),
        # 비전 제안. 비전을 부르지 않은 기록은 NULL ("none" 과 구분한다)
        sa.Column("ai_season", sa.String(length=10), nullable=True),
        sa.Column("ai_stage", sa.String(length=10), nullable=True),
        sa.Column("title", sa.String(length=100), nullable=True),
        # Supabase Storage 비공개 버킷 안의 객체 경로. 사진 자체는 DB 에 넣지 않는다
        sa.Column("image_path", sa.String(length=300), nullable=False),
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
            ["place_id"], ["places.id"], name=op.f("fk_records_place_id_places"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_records_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_records")),
    )
    op.create_index(op.f("ix_records_place_id"), "records", ["place_id"], unique=False)
    op.create_index(op.f("ix_records_user_id"), "records", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_records_user_id"), table_name="records")
    op.drop_index(op.f("ix_records_place_id"), table_name="records")
    op.drop_table("records")
