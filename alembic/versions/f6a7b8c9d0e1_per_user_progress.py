"""per-user media progress and bookmarks

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-07-13 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f6a7b8c9d0e1'
down_revision: Union[str, None] = 'e5f6a7b8c9d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Fresh-database assumption: existing rows are not migrated to a user.
    with op.batch_alter_table('media_progress', schema=None) as batch_op:
        batch_op.add_column(sa.Column('user_id', sa.Integer(), nullable=False))
        batch_op.drop_index(batch_op.f('ix_media_progress_book_id'))
        batch_op.create_index(batch_op.f('ix_media_progress_book_id'), ['book_id'],
                              unique=False)
        batch_op.create_index(batch_op.f('ix_media_progress_user_id'), ['user_id'],
                              unique=False)
        batch_op.create_unique_constraint('uq_media_progress_user_book',
                                          ['user_id', 'book_id'])
        batch_op.create_foreign_key('fk_media_progress_user_id', 'user', ['user_id'],
                                    ['id'], ondelete='CASCADE')

    with op.batch_alter_table('bookmark', schema=None) as batch_op:
        batch_op.add_column(sa.Column('user_id', sa.Integer(), nullable=False))
        batch_op.create_index(batch_op.f('ix_bookmark_user_id'), ['user_id'], unique=False)
        batch_op.create_foreign_key('fk_bookmark_user_id', 'user', ['user_id'], ['id'],
                                    ondelete='CASCADE')


def downgrade() -> None:
    with op.batch_alter_table('bookmark', schema=None) as batch_op:
        batch_op.drop_constraint('fk_bookmark_user_id', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_bookmark_user_id'))
        batch_op.drop_column('user_id')

    with op.batch_alter_table('media_progress', schema=None) as batch_op:
        batch_op.drop_constraint('fk_media_progress_user_id', type_='foreignkey')
        batch_op.drop_constraint('uq_media_progress_user_book', type_='unique')
        batch_op.drop_index(batch_op.f('ix_media_progress_user_id'))
        batch_op.drop_index(batch_op.f('ix_media_progress_book_id'))
        batch_op.create_index(batch_op.f('ix_media_progress_book_id'), ['book_id'],
                              unique=True)
        batch_op.drop_column('user_id')
