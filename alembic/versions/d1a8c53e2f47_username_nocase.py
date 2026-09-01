"""case-insensitive usernames

`user.username` gains SQLite's NOCASE collation, which makes its unique index
case-insensitive ("Dave" can no longer be registered beside "dave") and, for
free, makes every `username = ?` lookup fold case as well — logging in as
"DAVE" finds "dave". The stored value keeps whatever case was typed.

Existing rows that already collide only by case would make the unique index
impossible to build, so they are detected first and reported by name rather
than failing with SQLite's opaque "UNIQUE constraint failed".

Revision ID: d1a8c53e2f47
Revises: c9e4b27f1a63
Create Date: 2026-09-01 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd1a8c53e2f47'
down_revision: Union[str, None] = 'c9e4b27f1a63'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _reject_case_duplicates() -> None:
    clashes = op.get_bind().execute(
        sa.text(
            "SELECT group_concat(username, ', ') FROM user"
            " GROUP BY lower(username) HAVING count(*) > 1"
        )
    ).scalars().all()
    if clashes:
        raise RuntimeError(
            "these accounts differ only by capitalisation and cannot both exist "
            "once usernames are case-insensitive: "
            + "; ".join(clashes)
            + " — rename or remove one of each pair, then upgrade again"
        )


def upgrade() -> None:
    _reject_case_duplicates()
    # SQLite cannot alter a column in place; batch mode rebuilds the table and
    # recreates its indexes, picking up the new collation.
    with op.batch_alter_table("user") as batch_op:
        batch_op.alter_column(
            "username",
            existing_type=sa.String(length=100),
            type_=sa.String(length=100, collation="NOCASE"),
            existing_nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("user") as batch_op:
        batch_op.alter_column(
            "username",
            existing_type=sa.String(length=100, collation="NOCASE"),
            type_=sa.String(length=100),
            existing_nullable=False,
        )
