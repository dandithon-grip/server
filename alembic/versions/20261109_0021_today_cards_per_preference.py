"""today cards per preference

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-09 23:30:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0021"
down_revision: str | Sequence[str] | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 취향 칩 카드를 따로 캐시한다 (#101). 기존 (user, date, ride) 유일성에 preference 를 더한다
    op.drop_constraint("uq_today_cards_user_date_ride", "today_cards", type_="unique")
    op.execute(
        "ALTER TABLE today_cards ADD CONSTRAINT uq_today_cards_user_date_ride_pref "
        "UNIQUE NULLS NOT DISTINCT (user_id, date, ride_id, preference)"
    )


def downgrade() -> None:
    op.drop_constraint("uq_today_cards_user_date_ride_pref", "today_cards", type_="unique")
    op.execute(
        """
        DELETE FROM today_cards t USING today_cards other
        WHERE t.user_id = other.user_id AND t.date = other.date
          AND t.ride_id IS NOT DISTINCT FROM other.ride_id AND t.created_at < other.created_at
        """
    )
    op.execute(
        "ALTER TABLE today_cards ADD CONSTRAINT uq_today_cards_user_date_ride "
        "UNIQUE NULLS NOT DISTINCT (user_id, date, ride_id)"
    )
