"""media_progress hide_from_continue_listening

ABS clients offer "remove from continue listening" per item and per series
(GET /api/me/progress/:id/remove-from-continue-listening). Upstream stores the
flag on the progress row and clears it as soon as currentTime moves again, so
we need somewhere to keep it.

Revision ID: d5a3e91c4f27
Revises: c3f10a7d2b41
Create Date: 2026-08-05 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd5a3e91c4f27'
down_revision: Union[str, None] = 'c3f10a7d2b41'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "media_progress",
        sa.Column(
            "hide_from_continue_listening",
            sa.Boolean(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("media_progress", "hide_from_continue_listening")
