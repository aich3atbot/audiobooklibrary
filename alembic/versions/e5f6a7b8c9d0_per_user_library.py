"""per-user library: user_book, release attribution, strip Book user columns

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-07-13 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e5f6a7b8c9d0'
down_revision: Union[str, None] = 'd4e5f6a7b8c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

READ_STATES = sa.Enum(
    'none', 'want_to_read', 'reading', 'read', name='readstate', native_enum=False
)


def upgrade() -> None:
    op.create_table('user_book',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('book_id', sa.Integer(), nullable=False),
    sa.Column('hardcover_user_book_id', sa.Integer(), nullable=True),
    sa.Column('pending_push', sa.Boolean(), server_default='0', nullable=False),
    sa.Column('read_state', READ_STATES, nullable=False),
    sa.Column('read_at', sa.Date(), nullable=True),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['book_id'], ['book.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'book_id')
    )
    with op.batch_alter_table('user_book', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_user_book_book_id'), ['book_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_user_book_user_id'), ['user_id'], unique=False)

    with op.batch_alter_table('book', schema=None) as batch_op:
        batch_op.drop_column('hardcover_user_book_id')
        batch_op.drop_column('pending_push')
        batch_op.drop_column('read_state')
        batch_op.drop_column('read_at')

    with op.batch_alter_table('release', schema=None) as batch_op:
        batch_op.add_column(sa.Column('user_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('fk_release_user_id', 'user', ['user_id'], ['id'],
                                    ondelete='SET NULL')


def downgrade() -> None:
    with op.batch_alter_table('release', schema=None) as batch_op:
        batch_op.drop_constraint('fk_release_user_id', type_='foreignkey')
        batch_op.drop_column('user_id')

    with op.batch_alter_table('book', schema=None) as batch_op:
        batch_op.add_column(sa.Column('read_at', sa.Date(), nullable=True))
        batch_op.add_column(sa.Column('read_state', READ_STATES, nullable=False,
                                      server_default='none'))
        batch_op.add_column(sa.Column('pending_push', sa.Boolean(), server_default='0',
                                      nullable=False))
        batch_op.add_column(sa.Column('hardcover_user_book_id', sa.Integer(), nullable=True))

    with op.batch_alter_table('user_book', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_user_book_user_id'))
        batch_op.drop_index(batch_op.f('ix_user_book_book_id'))

    op.drop_table('user_book')
