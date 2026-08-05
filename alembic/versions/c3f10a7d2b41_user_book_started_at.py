"""user_book started_at

Listening now drives read state: crossing the "started" threshold in an ABS
client marks the book *currently reading* with today's date, which has to be
stored to be pushed as Hardcover's `first_started_reading_date` (and pulled
back from it). NULL means "no start date known".

Revision ID: c3f10a7d2b41
Revises: b1c7f4e28a10
Create Date: 2026-08-05 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c3f10a7d2b41'
down_revision: Union[str, None] = 'b1c7f4e28a10'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("user_book", sa.Column("started_at", sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column("user_book", "started_at")
