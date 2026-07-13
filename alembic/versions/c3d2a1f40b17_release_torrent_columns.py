"""release: torrent columns, drop Prowlarr ones

The app now searches a torrent indexer directly and hands the magnet to a
torrent client, so a release is identified by its details-page URL (guid) and
the torrent's info hash rather than by Prowlarr's guid + numeric indexer id.
Seeders go away with Prowlarr — AudioBookBay publishes no seeder counts.

Rows grabbed before this migration keep their guid, title and status, so
Activity history stays readable; their info_hash is null, which sends the
watcher down its name-matching fallback path.

Revision ID: c3d2a1f40b17
Revises: bf48efb57c13
Create Date: 2026-07-12

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c3d2a1f40b17"
down_revision: Union[str, None] = "bf48efb57c13"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("release") as batch_op:
        batch_op.alter_column("prowlarr_guid", new_column_name="guid")
        batch_op.add_column(
            sa.Column("indexer", sa.String(length=100), nullable=False, server_default="")
        )
        batch_op.add_column(sa.Column("info_hash", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("magnet_uri", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("progress", sa.Float(), nullable=True))
        batch_op.drop_column("indexer_id")
        batch_op.drop_column("seeders")
        batch_op.create_index("ix_release_info_hash", ["info_hash"])


def downgrade() -> None:
    with op.batch_alter_table("release") as batch_op:
        batch_op.drop_index("ix_release_info_hash")
        batch_op.add_column(
            sa.Column("indexer_id", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(sa.Column("seeders", sa.Integer(), nullable=True))
        batch_op.drop_column("progress")
        batch_op.drop_column("magnet_uri")
        batch_op.drop_column("info_hash")
        batch_op.drop_column("indexer")
        batch_op.alter_column("guid", new_column_name="prowlarr_guid")
