"""add jliga rating tables

Revision ID: b39d13df38ef
Revises: 7d9d7f4aa811
Create Date: 2026-11-26 17:58:38.461975

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b39d13df38ef'
down_revision: Union[str, Sequence[str], None] = '7d9d7f4aa811'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "rating_boards",
        sa.Column(
            "id",
            sa.Integer(),
            primary_key=True,
        ),
        sa.Column(
            "title",
            sa.String(length=150),
            nullable=False,
        ),
        sa.Column(
            "year",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "is_published",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text(
                "CURRENT_TIMESTAMP"
            ),
        ),
    )

    op.create_index(
        "ix_rating_boards_year",
        "rating_boards",
        ["year"],
    )

    op.create_table(
        "rating_columns",
        sa.Column(
            "id",
            sa.Integer(),
            primary_key=True,
        ),
        sa.Column(
            "board_id",
            sa.Integer(),
            sa.ForeignKey(
                "rating_boards.id",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),
        sa.Column(
            "tournament_id",
            sa.Integer(),
            sa.ForeignKey(
                "tournaments.id",
                ondelete="SET NULL",
            ),
            nullable=True,
        ),
        sa.Column(
            "label",
            sa.String(length=200),
            nullable=False,
        ),
        sa.Column(
            "position",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.UniqueConstraint(
            "board_id",
            "tournament_id",
            name="uq_rating_board_tournament",
        ),
    )

    op.create_index(
        "ix_rating_columns_board_id",
        "rating_columns",
        ["board_id"],
    )

    op.create_table(
        "rating_rows",
        sa.Column(
            "id",
            sa.Integer(),
            primary_key=True,
        ),
        sa.Column(
            "board_id",
            sa.Integer(),
            sa.ForeignKey(
                "rating_boards.id",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),
        sa.Column(
            "team_id",
            sa.Integer(),
            sa.ForeignKey(
                "teams.id",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),
        sa.Column(
            "points",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
        sa.UniqueConstraint(
            "board_id",
            "team_id",
            name="uq_rating_board_team",
        ),
    )

    op.create_index(
        "ix_rating_rows_board_id",
        "rating_rows",
        ["board_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_rating_rows_board_id",
        table_name="rating_rows",
    )

    op.drop_table("rating_rows")

    op.drop_index(
        "ix_rating_columns_board_id",
        table_name="rating_columns",
    )

    op.drop_table("rating_columns")

    op.drop_index(
        "ix_rating_boards_year",
        table_name="rating_boards",
    )

    op.drop_table("rating_boards")

