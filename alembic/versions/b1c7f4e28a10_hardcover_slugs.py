"""hardcover slugs

hardcover.app routes on slugs, not ids, so linking a book or series out to
Hardcover needs the slug stored. NULL means "never looked up" — the sync
backfill fills those in and writes "" for rows Hardcover has no slug for.

Revision ID: b1c7f4e28a10
Revises: 4bd517888da0
Create Date: 2026-07-29 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b1c7f4e28a10'
down_revision: Union[str, None] = '4bd517888da0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("book", sa.Column("hardcover_slug", sa.Text(), nullable=True))
    op.add_column("series", sa.Column("hardcover_slug", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("series", "hardcover_slug")
    op.drop_column("book", "hardcover_slug")
