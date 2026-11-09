"""today cards per ride

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-09 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0018"
down_revision: str | Sequence[str] | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 탑승 계획별 카드 (#90). 기본 카드는 ride_id NULL 이고 NULL 도 하나로 세어 하루 1장을 유지한다
    op.add_column("today_cards", sa.Column("ride_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_today_cards_ride_id_commute_rides",
        "today_cards",
        "commute_rides",
        ["ride_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.drop_constraint("uq_today_cards_user_id_date", "today_cards", type_="unique")
    op.execute(
        "ALTER TABLE today_cards ADD CONSTRAINT uq_today_cards_user_date_ride "
        "UNIQUE NULLS NOT DISTINCT (user_id, date, ride_id)"
    )


def downgrade() -> None:
    op.drop_constraint("uq_today_cards_user_date_ride", "today_cards", type_="unique")
    op.execute("DELETE FROM today_cards WHERE ride_id IS NOT NULL")
    op.create_unique_constraint("uq_today_cards_user_id_date", "today_cards", ["user_id", "date"])
    op.drop_constraint("fk_today_cards_ride_id_commute_rides", "today_cards", type_="foreignkey")
    op.drop_column("today_cards", "ride_id")
