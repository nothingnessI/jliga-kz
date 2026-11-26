"""add second yellow card event type

Revision ID: 7d9d7f4aa811
Revises: fdcc348dd30d
Create Date: 2026-11-26 12:17:05.646114

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7d9d7f4aa811'
down_revision: Union[str, Sequence[str], None] = 'fdcc348dd30d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None



def upgrade() -> None:
    op.execute(
        "ALTER TYPE match_event_type "
        "ADD VALUE IF NOT EXISTS 'SECOND_YELLOW_CARD'"
    )


def downgrade() -> None:
    # PostgreSQL не поддерживает простое удаление
    # отдельного значения из ENUM.
    pass
