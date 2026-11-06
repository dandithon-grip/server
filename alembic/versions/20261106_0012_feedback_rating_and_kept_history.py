"""feedback rating and kept history

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-06 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0012"
down_revision: str | Sequence[str] | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 👍/👎 → 별점 1~5 (#50). 기존 값은 up → 5, down → 1
    op.add_column("feedback", sa.Column("rating", sa.SmallInteger(), nullable=True))
    op.execute("UPDATE feedback SET rating = CASE thumbs WHEN 'up' THEN 5 ELSE 1 END")
    op.alter_column("feedback", "rating", nullable=False)
    op.create_check_constraint("ck_feedback_rating_range", "feedback", "rating BETWEEN 1 AND 5")
    # thumbs 는 rating 으로 옮겼으므로 지운다 (자동 생성 drop 이 아니라 의도한 drop)
    op.drop_column("feedback", "thumbs")

    # 평가 당시 태그를 남긴다. 기존 행은 경로의 저장된 태그에서 채운다 (view 는 가중치 태그가 아님)
    op.add_column(
        "feedback",
        sa.Column(
            "tags",
            postgresql.ARRAY(sa.String(length=10)),
            server_default="{}",
            nullable=False,
        ),
    )
    op.execute(
        "UPDATE feedback f SET tags = array_remove(r.tags, 'view') FROM routes r "
        "WHERE r.id = f.route_id"
    )

    # 경로가 다시 만들어져도 평가 기록은 남긴다: CASCADE → SET NULL
    op.drop_constraint("fk_feedback_route_id_routes", "feedback", type_="foreignkey")
    op.alter_column("feedback", "route_id", existing_type=sa.Uuid(), nullable=True)
    op.create_foreign_key(
        "fk_feedback_route_id_routes",
        "feedback",
        "routes",
        ["route_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_feedback_route_id_routes", "feedback", type_="foreignkey")
    op.execute("DELETE FROM feedback WHERE route_id IS NULL")
    op.alter_column("feedback", "route_id", existing_type=sa.Uuid(), nullable=False)
    op.create_foreign_key(
        "fk_feedback_route_id_routes",
        "feedback",
        "routes",
        ["route_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.drop_column("feedback", "tags")
    op.add_column("feedback", sa.Column("thumbs", sa.String(length=5), nullable=True))
    op.execute("UPDATE feedback SET thumbs = CASE WHEN rating >= 3 THEN 'up' ELSE 'down' END")
    op.alter_column("feedback", "thumbs", nullable=False)
    op.drop_constraint("ck_feedback_rating_range", "feedback", type_="check")
    op.drop_column("feedback", "rating")
