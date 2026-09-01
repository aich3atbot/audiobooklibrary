"""auth session kind

Browser logins move into `auth_session` alongside the ABS refresh-token
sessions, so a UI session can be revoked (a signed cookie has nothing to
revoke). `kind` tells the two apart: existing rows are all ABS clients.

The admin account also becomes a real `user` row from here on, but that is not
a schema change — `user.role` is a non-native enum stored as VARCHAR(7) with no
check constraint, so the new "admin" value needs no migration. The row itself
is created at startup from ADMIN_PASSWORD
(`app.services.users.ensure_admin_account`), which has to run every boot
anyway.

Revision ID: c9e4b27f1a63
Revises: b6f3d81a92c4
Create Date: 2026-09-01 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c9e4b27f1a63'
down_revision: Union[str, None] = 'b6f3d81a92c4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "auth_session",
        sa.Column("kind", sa.String(length=8), nullable=False, server_default="abs"),
    )


def downgrade() -> None:
    op.drop_column("auth_session", "kind")
