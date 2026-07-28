"""author image url

Hardcover's author photo, served to ABS clients as /api/authors/:id/image.
NULL means "never looked up" — the sync backfill fills those in and writes ""
for authors Hardcover has no photo for.

Revision ID: 4bd517888da0
Revises: 4279694b0300
Create Date: 2026-07-28 00:10:04.693676

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '4bd517888da0'
down_revision: Union[str, None] = '4279694b0300'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("author", sa.Column("image_url", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("author", "image_url")
