"""fix tournament logo_url nullable and standings unique constraint

Revision ID: a1b2c3d4e5f6
Revises: eadb918fc2f5
Create Date: 2026-11-09

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = 'eadb918fc2f5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. logo_url в tournaments должен быть nullable (как в модели)
    op.alter_column(
        'tournaments', 'logo_url',
        existing_type=sa.String(length=500),
        nullable=True
    )

    # 2. Уникальность строки таблицы должна учитывать группу
    #    (модель: tournament_id + team_id + group_name)
    op.drop_constraint('uq_standing_tournament_team', 'tournament_standings', type_='unique')
    op.create_unique_constraint(
        'uq_standing_tournament_team',
        'tournament_standings',
        ['tournament_id', 'team_id', 'group_name']
    )


def downgrade() -> None:
    op.drop_constraint('uq_standing_tournament_team', 'tournament_standings', type_='unique')
    op.create_unique_constraint(
        'uq_standing_tournament_team',
        'tournament_standings',
        ['tournament_id', 'team_id']
    )
    op.alter_column(
        'tournaments', 'logo_url',
        existing_type=sa.String(length=500),
        nullable=False
    )