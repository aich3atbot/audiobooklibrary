"""user role

Accounts gain a role: "full" (everything, as before) or "limited" — an
ABS-only listener with no web UI access and no Hardcover token. Existing rows
are full accounts.

Revision ID: f8d2a63b7c14
Revises: e7c2b84a15d9
Create Date: 2026-08-06 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f8d2a63b7c14'
down_revision: Union[str, None] = 'e7c2b84a15d9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "user",
        sa.Column(
            "role",
            sa.Enum("full", "limited", name="userrole", native_enum=False),
            nullable=False,
            server_default="full",
        ),
    )


def downgrade() -> None:
    op.drop_column("user", "role")
