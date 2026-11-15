"""merge multiple heads

Revision ID: d57295cffd53
Revises: 8513817ad0e6, fix_tz_naive
Create Date: 2026-11-14 20:37:38.536997

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd57295cffd53'
down_revision: Union[str, Sequence[str], None] = ('8513817ad0e6', 'fix_tz_naive')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
