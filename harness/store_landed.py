"""Handler for the mark-landed command: stamp the landed fields on a stored task record.

The record's updated_at column is passed through unchanged, because this module reads no clock.
unknown: whether updated_at should track the landed time instead.
"""

from __future__ import annotations

from typing import Any

from harness.store_dialect import Connection, json_load
from harness.store_write import Store

# Mirror what `cox runs land` writes into runs/<run>/tasks/<phase>/<task>.json (another repo).
LANDED_KEY = "landed"
LANDED_PR = "pr"
LANDED_AT = "at"


def _fetch(conn: Connection, run_id: str, phase: str, task: str) -> tuple[dict[str, Any], str] | None:
    """The stored record and its updated_at, or None when the key has no row."""
    p = conn.dialect.placeholder
    sql = f"SELECT record_json, updated_at FROM task_records WHERE run_id = {p} AND phase_id = {p} AND task_id = {p}"
    row = conn.query_one(sql, (run_id, phase, task))
    return None if row is None else (json_load(row[0]), row[1])


def _with_landed(record: dict[str, Any], pr: str, at: str) -> dict[str, Any]:
    """A copy of the record whose landed object is replaced whole."""
    return {**record, LANDED_KEY: {LANDED_PR: pr, LANDED_AT: at}}


def mark_landed(conn: Connection, run_id: str, phase: str, task: str, pr: str, at: str) -> dict[str, Any] | None:
    """Set the landed fields on a task record; None, and no write, when the record is absent."""
    found = _fetch(conn, run_id, phase, task)
    if found is None:
        return None
    record, updated_at = found
    updated = _with_landed(record, pr, at)
    Store(conn).record_task_record(run_id, phase, task, updated, updated_at)
    return updated
