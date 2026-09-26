"""set_state: record a work item's state, keeping the phase and needs it already has."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from harness.store_dialect import Connection, insert_ignore, json_text
from harness.store_read import work_items
from harness.store_write import upsert_work_item, work_item_row

ABSENT = "<absent>"


@dataclass(frozen=True)
class Mismatch:
    """A compare-and-set that did not apply: `current` is the row's actual state, None when it has no row."""

    current: str | None


def _current(conn: Connection, initiative: str, task: str) -> dict[str, Any] | None:
    return next((r for r in work_items(conn, initiative) if r["task_id"] == task), None)


def _written(row: dict[str, Any]) -> dict[str, Any]:
    keys = ("initiative", "task_id", "phase", "state", "updated_at", "updated_by")
    return {**{k: row[k] for k in keys}, "needs": list(row["needs"])}


def _cas(
    conn: Connection, initiative: str, task: str, state: str, by: str, phase: str | None, now: str, expected: str
) -> dict[str, Any] | Mismatch | None:
    """The compare lives in the write's own statement, so two writers cannot both match."""
    mark = conn.dialect.placeholder
    with conn.transaction():
        if expected == ABSENT:
            row = work_item_row(initiative, task, phase or "", state, [], now, by)
            sql = insert_ignore(conn.dialect, "work_items", list(row), ("initiative", "task_id"))
            params = [json_text(v) if k == "needs_json" else v for k, v in row.items()]
            applied = phase is not None and conn.execute(sql, params) == 1
        else:
            sql = (
                f"UPDATE work_items SET state = {mark}, updated_at = {mark}, updated_by = {mark} "
                f"WHERE initiative = {mark} AND task_id = {mark} AND state = {mark}"
            )
            applied = conn.execute(sql, [state, now, by, initiative, task, expected]) == 1
        current = _current(conn, initiative, task)
    if current is None:
        return None if expected == ABSENT else Mismatch(None)
    return _written(current) if applied else Mismatch(current["state"])


def set_state(
    conn: Connection,
    initiative: str,
    task: str,
    state: str,
    by: str,
    phase: str | None,
    now: str,
    *,
    expected: str | None = None,
) -> dict[str, Any] | Mismatch | None:
    """The row as written; with `expected`, only if the state equals it, else Mismatch, and a missing row is Mismatch(None) unless expected is ABSENT."""
    if expected is not None:
        return _cas(conn, initiative, task, state, by, phase, now, expected)
    current = _current(conn, initiative, task)
    if current is None and phase is None:
        return None
    kept_phase, needs = (phase, []) if current is None else (current["phase"], current["needs"])
    upsert_work_item(conn, initiative, task, kept_phase, state, needs, now, by)
    return {
        "initiative": initiative,
        "task_id": task,
        "phase": kept_phase,
        "state": state,
        "needs": list(needs),
        "updated_at": now,
        "updated_by": by,
    }
