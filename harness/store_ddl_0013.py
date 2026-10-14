"""Migration 0013: card keys on work_items.

issue_number is an INTEGER, project_item_id and parent_item are TEXT. All three are nullable
with no default, no index and no foreign-key clause, so an existing row reads NULL.
Nothing writes or reads the columns yet, and nothing backfills them here, only the schema.
"""

from __future__ import annotations

from harness.store_dialect import Dialect

VERSION = 13
DESCRIPTION = "work_items gains issue_number, project_item_id, parent_item"

# Table and column pairs that the ALTERs append; store_copy reads this 2-tuple shape, matching ddl2/ddl3/ddl5/ddl9/ddl10/ddl11/ddl12.
_ADDED: tuple[tuple[str, str], ...] = (
    ("work_items", "issue_number"),
    ("work_items", "project_item_id"),
    ("work_items", "parent_item"),
)


def statements(dialect: Dialect) -> tuple[str, ...]:
    """Three ALTERs, the same on both dialects."""
    return (
        "ALTER TABLE work_items ADD COLUMN issue_number INTEGER",
        "ALTER TABLE work_items ADD COLUMN project_item_id TEXT",
        "ALTER TABLE work_items ADD COLUMN parent_item TEXT",
    )
