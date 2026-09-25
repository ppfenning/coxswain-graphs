"""Migration 0005: attempts.cause and attempts.cause_why.

Both columns are nullable TEXT with no default and no constraint. Existing rows keep NULL.
"""

from __future__ import annotations

from harness.store_dialect import Dialect

VERSION = 5
DESCRIPTION = "attempts gains cause and cause_why"


# Table and TEXT column that the ALTERs append to a migration 0001 table.
_ADDED: tuple[tuple[str, str], ...] = (("attempts", "cause"), ("attempts", "cause_why"))


def statements(dialect: Dialect) -> tuple[str, ...]:
    """The same ALTERs on both dialects."""
    return tuple(f"ALTER TABLE {t} ADD COLUMN {c} TEXT" for t, c in _ADDED)
