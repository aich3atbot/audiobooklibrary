"""editions: a book can hold several audiobook recordings

Creates the edition table, moves download_state/library_path off book, and
repoints release/audio_file/media_progress/bookmark from book_id to
edition_id. Every book with pipeline state gets one unlabelled edition; the
unlabelled edition's library path is identical to the old book path, so this
migration never touches files on disk.

The dependent tables are rebuilt explicitly (create/copy/drop/rename) instead
of batch_alter_table: their UNIQUE constraints are unnamed, which SQLite
reflection can't drop by name.

Revision ID: 7e21c3a90d44
Revises: 91acd8a782ef
Create Date: 2026-07-18

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '7e21c3a90d44'
down_revision: Union[str, None] = '91acd8a782ef'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DOWNLOAD_STATE = sa.Enum(
    'none', 'grabbed', 'downloading', 'imported', 'failed',
    name='downloadstate', native_enum=False,
)
# The pre-edition enum carried two never-assigned members.
OLD_DOWNLOAD_STATE = sa.Enum(
    'none', 'wanted', 'grabbed', 'downloading', 'downloaded', 'imported', 'failed',
    name='downloadstate', native_enum=False,
)


def upgrade() -> None:
    op.create_table('edition',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('book_id', sa.Integer(), nullable=False),
    sa.Column('hardcover_edition_id', sa.Integer(), nullable=True),
    sa.Column('label', sa.String(length=200), server_default='', nullable=False),
    sa.Column('narrator', sa.String(length=500), server_default='', nullable=False),
    sa.Column('download_state', DOWNLOAD_STATE, nullable=False),
    sa.Column('library_path', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['book_id'], ['book.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('book_id', 'label')
    )
    with op.batch_alter_table('edition', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_edition_book_id'), ['book_id'], unique=False)

    # One unlabelled edition per book that has any pipeline state hanging off
    # it. wanted/downloaded were never written, so no mapping is needed for
    # them; pure-metadata books get no edition row (created at first grab).
    op.execute("""
        INSERT INTO edition (book_id, hardcover_edition_id, label, narrator,
                             download_state, library_path)
        SELECT id, NULL, '', '', download_state, library_path FROM book
        WHERE library_path IS NOT NULL OR download_state != 'none'
           OR id IN (SELECT book_id FROM release
                     UNION SELECT book_id FROM audio_file
                     UNION SELECT book_id FROM media_progress
                     UNION SELECT book_id FROM bookmark)
    """)

    op.create_table('release_new',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('edition_id', sa.Integer(), nullable=True),
    sa.Column('user_id', sa.Integer(), nullable=True),
    sa.Column('guid', sa.Text(), nullable=False),
    sa.Column('indexer', sa.String(length=100), nullable=False),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('size', sa.Integer(), nullable=True),
    sa.Column('info_hash', sa.String(length=64), nullable=True),
    sa.Column('magnet_uri', sa.Text(), nullable=True),
    sa.Column('progress', sa.Float(), nullable=True),
    sa.Column('grabbed_at', sa.DateTime(), nullable=True),
    sa.Column('status', sa.String(length=50), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['edition_id'], ['edition.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.execute("""
        INSERT INTO release_new (id, edition_id, user_id, guid, indexer, title,
                                 size, info_hash, magnet_uri, progress,
                                 grabbed_at, status, error)
        SELECT r.id, (SELECT e.id FROM edition e WHERE e.book_id = r.book_id),
               r.user_id, r.guid, r.indexer, r.title, r.size, r.info_hash,
               r.magnet_uri, r.progress, r.grabbed_at, r.status, r.error
        FROM release r
    """)
    op.drop_table('release')
    op.rename_table('release_new', 'release')
    with op.batch_alter_table('release', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_release_edition_id'), ['edition_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_release_info_hash'), ['info_hash'], unique=False)

    op.create_table('audio_file_new',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('edition_id', sa.Integer(), nullable=False),
    sa.Column('index', sa.Integer(), nullable=False),
    sa.Column('rel_path', sa.Text(), nullable=False),
    sa.Column('size', sa.Integer(), nullable=False),
    sa.Column('mtime_ms', sa.Integer(), nullable=False),
    sa.Column('duration', sa.Float(), nullable=True),
    sa.Column('mime_type', sa.String(length=50), nullable=False),
    sa.Column('chapters_json', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['edition_id'], ['edition.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.execute("""
        INSERT INTO audio_file_new (id, edition_id, "index", rel_path, size,
                                    mtime_ms, duration, mime_type, chapters_json)
        SELECT f.id, (SELECT e.id FROM edition e WHERE e.book_id = f.book_id),
               f."index", f.rel_path, f.size, f.mtime_ms, f.duration,
               f.mime_type, f.chapters_json
        FROM audio_file f
    """)
    op.drop_table('audio_file')
    op.rename_table('audio_file_new', 'audio_file')
    with op.batch_alter_table('audio_file', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_audio_file_edition_id'), ['edition_id'], unique=False)

    op.create_table('media_progress_new',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('edition_id', sa.Integer(), nullable=False),
    sa.Column('current_time', sa.Float(), nullable=False),
    sa.Column('duration', sa.Float(), nullable=False),
    sa.Column('is_finished', sa.Boolean(), server_default='0', nullable=False),
    sa.Column('started_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('finished_at', sa.DateTime(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['edition_id'], ['edition.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'edition_id')
    )
    op.execute("""
        INSERT INTO media_progress_new (id, user_id, edition_id, current_time,
                                        duration, is_finished, started_at,
                                        finished_at, updated_at)
        SELECT p.id, p.user_id,
               (SELECT e.id FROM edition e WHERE e.book_id = p.book_id),
               p.current_time, p.duration, p.is_finished, p.started_at,
               p.finished_at, p.updated_at
        FROM media_progress p
    """)
    op.drop_table('media_progress')
    op.rename_table('media_progress_new', 'media_progress')
    with op.batch_alter_table('media_progress', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_media_progress_edition_id'), ['edition_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_media_progress_user_id'), ['user_id'], unique=False)

    op.create_table('bookmark_new',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('edition_id', sa.Integer(), nullable=False),
    sa.Column('time', sa.Float(), nullable=False),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['edition_id'], ['edition.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.execute("""
        INSERT INTO bookmark_new (id, user_id, edition_id, time, title, created_at)
        SELECT b.id, b.user_id,
               (SELECT e.id FROM edition e WHERE e.book_id = b.book_id),
               b.time, b.title, b.created_at
        FROM bookmark b
    """)
    op.drop_table('bookmark')
    op.rename_table('bookmark_new', 'bookmark')
    with op.batch_alter_table('bookmark', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_bookmark_edition_id'), ['edition_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_bookmark_user_id'), ['user_id'], unique=False)

    with op.batch_alter_table('book', schema=None) as batch_op:
        batch_op.drop_column('download_state')
        batch_op.drop_column('library_path')


def downgrade() -> None:
    """Best effort: collapse each book's first edition back onto book."""
    with op.batch_alter_table('book', schema=None) as batch_op:
        batch_op.add_column(sa.Column('download_state', OLD_DOWNLOAD_STATE, nullable=False, server_default='none'))
        batch_op.add_column(sa.Column('library_path', sa.Text(), nullable=True))
    op.execute("""
        UPDATE book SET
            download_state = COALESCE(
                (SELECT e.download_state FROM edition e
                 WHERE e.book_id = book.id ORDER BY e.id LIMIT 1), 'none'),
            library_path =
                (SELECT e.library_path FROM edition e
                 WHERE e.book_id = book.id ORDER BY e.id LIMIT 1)
    """)

    def rebuild_with_book_id(table: str, columns: list[sa.Column], copy_cols: str,
                             constraints: list, indexes: list[str]) -> None:
        op.create_table(f'{table}_new', *columns, *constraints)
        op.execute(f"""
            INSERT INTO {table}_new (id, book_id, {copy_cols})
            SELECT t.id, (SELECT e.book_id FROM edition e WHERE e.id = t.edition_id),
                   {', '.join('t.' + c.strip() for c in copy_cols.split(','))}
            FROM {table} t
        """)
        op.drop_table(table)
        op.rename_table(f'{table}_new', table)
        with op.batch_alter_table(table, schema=None) as batch_op:
            for index_col in indexes:
                batch_op.create_index(batch_op.f(f'ix_{table}_{index_col}'), [index_col], unique=False)

    rebuild_with_book_id(
        'release',
        [sa.Column('id', sa.Integer(), nullable=False),
         sa.Column('book_id', sa.Integer(), nullable=False),
         sa.Column('user_id', sa.Integer(), nullable=True),
         sa.Column('guid', sa.Text(), nullable=False),
         sa.Column('indexer', sa.String(length=100), nullable=False),
         sa.Column('title', sa.Text(), nullable=False),
         sa.Column('size', sa.Integer(), nullable=True),
         sa.Column('info_hash', sa.String(length=64), nullable=True),
         sa.Column('magnet_uri', sa.Text(), nullable=True),
         sa.Column('progress', sa.Float(), nullable=True),
         sa.Column('grabbed_at', sa.DateTime(), nullable=True),
         sa.Column('status', sa.String(length=50), nullable=False),
         sa.Column('error', sa.Text(), nullable=True)],
        'user_id, guid, indexer, title, size, info_hash, magnet_uri, progress, grabbed_at, status, error',
        [sa.ForeignKeyConstraint(['book_id'], ['book.id'], ),
         sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='SET NULL'),
         sa.PrimaryKeyConstraint('id')],
        ['book_id', 'info_hash'],
    )
    rebuild_with_book_id(
        'audio_file',
        [sa.Column('id', sa.Integer(), nullable=False),
         sa.Column('book_id', sa.Integer(), nullable=False),
         sa.Column('index', sa.Integer(), nullable=False),
         sa.Column('rel_path', sa.Text(), nullable=False),
         sa.Column('size', sa.Integer(), nullable=False),
         sa.Column('mtime_ms', sa.Integer(), nullable=False),
         sa.Column('duration', sa.Float(), nullable=True),
         sa.Column('mime_type', sa.String(length=50), nullable=False),
         sa.Column('chapters_json', sa.Text(), nullable=True)],
        '"index", rel_path, size, mtime_ms, duration, mime_type, chapters_json',
        [sa.ForeignKeyConstraint(['book_id'], ['book.id'], ),
         sa.PrimaryKeyConstraint('id')],
        ['book_id'],
    )
    rebuild_with_book_id(
        'media_progress',
        [sa.Column('id', sa.Integer(), nullable=False),
         sa.Column('user_id', sa.Integer(), nullable=False),
         sa.Column('book_id', sa.Integer(), nullable=False),
         sa.Column('current_time', sa.Float(), nullable=False),
         sa.Column('duration', sa.Float(), nullable=False),
         sa.Column('is_finished', sa.Boolean(), server_default='0', nullable=False),
         sa.Column('started_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
         sa.Column('finished_at', sa.DateTime(), nullable=True),
         sa.Column('updated_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False)],
        'user_id, current_time, duration, is_finished, started_at, finished_at, updated_at',
        [sa.ForeignKeyConstraint(['book_id'], ['book.id'], ),
         sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='CASCADE'),
         sa.PrimaryKeyConstraint('id'),
         sa.UniqueConstraint('user_id', 'book_id')],
        ['book_id', 'user_id'],
    )
    rebuild_with_book_id(
        'bookmark',
        [sa.Column('id', sa.Integer(), nullable=False),
         sa.Column('user_id', sa.Integer(), nullable=False),
         sa.Column('book_id', sa.Integer(), nullable=False),
         sa.Column('time', sa.Float(), nullable=False),
         sa.Column('title', sa.Text(), nullable=False),
         sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False)],
        'user_id, time, title, created_at',
        [sa.ForeignKeyConstraint(['book_id'], ['book.id'], ),
         sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='CASCADE'),
         sa.PrimaryKeyConstraint('id')],
        ['book_id', 'user_id'],
    )

    with op.batch_alter_table('edition', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_edition_book_id'))
    op.drop_table('edition')
