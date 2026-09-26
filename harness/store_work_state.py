"""set_state: record a work item's state, keeping the phase and needs it already has."""

from __future__ import annotations

from typing import Any

from harness.store_dialect import Connection
from harness.store_read import work_items
from harness.store_write import upsert_work_item


def set_state(
    conn: Connection,
    initiative: str,
    task: str,
    state: str,
    by: str,
    phase: str | None,
    now: str,
) -> dict[str, Any] | None:
    """The row as written. None, with nothing written, when no row exists and no phase was given."""
    current = next((r for r in work_items(conn, initiative) if r["task_id"] == task), None)
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
