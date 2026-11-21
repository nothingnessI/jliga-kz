
"""Fix coach role enum.

Revision ID: 1ea283a3d185
Revises: eb6f5cd53c8a
"""

from typing import Sequence, Union

from alembic import op


revision: str = "1ea283a3d185"
down_revision: Union[str, Sequence[str], None] = "eb6f5cd53c8a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TYPE user_role ADD VALUE IF NOT EXISTS 'COACH'"
    )


def downgrade() -> None:
    pass
