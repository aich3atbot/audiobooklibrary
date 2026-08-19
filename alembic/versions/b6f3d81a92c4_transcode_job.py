"""transcode job

Converting an MP3 edition into a single chaptered m4b outlives the request that
asks for it and deletes files when it succeeds, so it gets a row: the Activity
page reads failures from here, and a job left running by a restart is failed
from here on the next startup rather than silently resumed.

Revision ID: b6f3d81a92c4
Revises: a4e1c7b90d33
Create Date: 2026-08-19 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b6f3d81a92c4'
down_revision: Union[str, None] = 'a4e1c7b90d33'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "transcode_job",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("edition_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column(
            "state",
            sa.Enum(
                "queued", "running", "done", "failed", "cancelled",
                name="transcodestate", native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("progress", sa.Float(), nullable=True),
        sa.Column(
            "cancel_requested", sa.Boolean(), nullable=False, server_default="0"
        ),
        sa.Column("source_count", sa.Integer(), nullable=True),
        sa.Column("bitrate", sa.Integer(), nullable=True),
        sa.Column("output_path", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["edition_id"], ["edition.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_transcode_job_edition_id"), "transcode_job", ["edition_id"]
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_transcode_job_edition_id"), table_name="transcode_job")
    op.drop_table("transcode_job")
