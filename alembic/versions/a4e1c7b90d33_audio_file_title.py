"""audio file title tag

Keep each track's own title tag (ID3 TIT2 / MP4 ©nam) so a file whose
*filename* names nothing but a number can still title its chapter. Existing
rows are backfilled by the startup re-scan (CHAPTER_SCAN_VERSION), not here —
the value has to be read out of the audio files themselves.

Revision ID: a4e1c7b90d33
Revises: f8d2a63b7c14
Create Date: 2026-08-19 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a4e1c7b90d33'
down_revision: Union[str, None] = 'f8d2a63b7c14'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("audio_file", sa.Column("title", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("audio_file", "title")
