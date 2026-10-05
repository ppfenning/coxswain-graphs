"""Migration 0012: priority on work_items.

priority is an INTEGER that defaults to 0, higher first, so an existing row reads 0
rather than NULL. No index, no foreign-key clause, matching every migration since 0002.
Nothing writes or reads the column yet, only the schema.
"""

from __future__ import annotations

from harness.store_dialect import Dialect

VERSION = 12
DESCRIPTION = "work_items gains priority"

# Table and column that the ALTER appends; store_copy reads this 2-tuple shape, matching ddl2/ddl3/ddl5/ddl9/ddl10/ddl11.
_ADDED: tuple[tuple[str, str], ...] = (("work_items", "priority"),)


def statements(dialect: Dialect) -> tuple[str, ...]:
    """One ALTER, the same on both dialects."""
    return ("ALTER TABLE work_items ADD COLUMN priority INTEGER NOT NULL DEFAULT 0",)
