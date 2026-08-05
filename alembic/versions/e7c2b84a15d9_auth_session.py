"""auth_session

Refresh tokens are stateless JWTs, so signing out could not revoke anything —
a lost device kept working for the token's full 30 days. One row per
long-lived client login gives logout (and per-device revocation via
/api/me/sessions) something to delete. Only SHA-256 hashes of the tokens are
stored.

Revision ID: e7c2b84a15d9
Revises: d5a3e91c4f27
Create Date: 2026-08-05 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e7c2b84a15d9'
down_revision: Union[str, None] = 'd5a3e91c4f27'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "auth_session",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("uuid", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("last_token_hash", sa.String(length=64), nullable=True),
        sa.Column("last_token_expires_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"),
                  nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("uuid"),
    )
    op.create_index(op.f("ix_auth_session_user_id"), "auth_session", ["user_id"])
    op.create_index(op.f("ix_auth_session_token_hash"), "auth_session", ["token_hash"])
    op.create_index(
        op.f("ix_auth_session_last_token_hash"), "auth_session", ["last_token_hash"]
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_auth_session_last_token_hash"), table_name="auth_session")
    op.drop_index(op.f("ix_auth_session_token_hash"), table_name="auth_session")
    op.drop_index(op.f("ix_auth_session_user_id"), table_name="auth_session")
    op.drop_table("auth_session")
