"""Add missing indexes and drop duplicate indexes

Adds indexes that the models declare but the base schema migration never created
(MediaItem.requested_at, Season.parent_id, Episode.parent_id) and drops duplicate
indexes that were created under two different names for the same column on
SubtitleEntry and MediaEntry.

Revision ID: c4d8f1a9b3e7
Revises: b1345f835923
Create Date: 2026-06-15 12:00:00.000000

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4d8f1a9b3e7"
down_revision: Union[str, None] = "b1345f835923"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Indexes that should exist (model declares them, base schema omitted them).
# (index_name, table_name, column_name)
_MISSING_INDEXES = [
    ("ix_mediaitem_requested_at", "MediaItem", "requested_at"),
    ("ix_Season_parent_id", "Season", "parent_id"),
    ("ix_Episode_parent_id", "Episode", "parent_id"),
]

# Duplicate indexes to remove. The lowercase ``ix_subtitle_entry_*`` /
# ``ix_media_entry_*`` variants (declared in each model's ``__table_args__``)
# are kept; these PascalCase duplicates created from ``index=True`` are dropped.
# (index_name, table_name, column_name)  -- column used only for downgrade.
_DUPLICATE_INDEXES = [
    ("ix_SubtitleEntry_language", "SubtitleEntry", "language"),
    ("ix_SubtitleEntry_parent_original_filename", "SubtitleEntry", "parent_original_filename"),
    ("ix_SubtitleEntry_file_hash", "SubtitleEntry", "file_hash"),
    ("ix_SubtitleEntry_opensubtitles_id", "SubtitleEntry", "opensubtitles_id"),
    ("ix_MediaEntry_original_filename", "MediaEntry", "original_filename"),
]


def upgrade() -> None:
    for index_name, table_name, column_name in _MISSING_INDEXES:
        op.execute(
            f'CREATE INDEX IF NOT EXISTS "{index_name}" '
            f'ON "{table_name}" ("{column_name}")'
        )

    for index_name, _table_name, _column_name in _DUPLICATE_INDEXES:
        op.execute(f'DROP INDEX IF EXISTS "{index_name}"')


def downgrade() -> None:
    # Recreate the duplicate indexes that upgrade() removed.
    for index_name, table_name, column_name in _DUPLICATE_INDEXES:
        op.execute(
            f'CREATE INDEX IF NOT EXISTS "{index_name}" '
            f'ON "{table_name}" ("{column_name}")'
        )

    for index_name, _table_name, _column_name in _MISSING_INDEXES:
        op.execute(f'DROP INDEX IF EXISTS "{index_name}"')
