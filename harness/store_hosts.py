"""Host command handlers: the hosts table as rows, each write a function of the connection and `now`.

An unknown host is None, not an error. Nothing here reads the clock.
"""

from __future__ import annotations

from typing import Any

from harness.store_dialect import Connection, json_load, json_text, upsert

__all__ = ["HOST_STATES", "host_beat", "host_list", "host_set_state", "host_upsert"]

HOST_STATES = ("active", "draining", "offline")
HOST_COLUMNS = ("name", "ssh", "capacity", "state", "beat_at", "versions_json", "updated_at", "updated_by")
HOST_KEY = ("name",)


def _row(values: tuple[Any, ...]) -> dict[str, Any]:
    return {c: json_load(v) if c == "versions_json" else v for c, v in zip(HOST_COLUMNS, values, strict=True)}


def _find(conn: Connection, name: str) -> dict[str, Any] | None:
    mark = conn.dialect.placeholder
    found = conn.query_all(f"SELECT {', '.join(HOST_COLUMNS)} FROM hosts WHERE name = {mark}", [name])
    return _row(found[0]) if found else None


def host_upsert(conn: Connection, name: str, ssh: str, capacity: int, state: str, by: str, now: str) -> dict[str, Any]:
    """Insert or overwrite the row. beat_at and versions_json stay as they were, "" and {} on insert."""
    with conn.transaction():
        kept = _find(conn, name) or {"beat_at": "", "versions_json": {}}
        row = {
            "name": name,
            "ssh": ssh,
            "capacity": capacity,
            "state": state,
            "beat_at": kept["beat_at"],
            "versions_json": kept["versions_json"],
            "updated_at": now,
            "updated_by": by,
        }
        params = [json_text(v) if c == "versions_json" else v for c, v in row.items()]
        conn.execute(upsert(conn.dialect, "hosts", HOST_COLUMNS, HOST_KEY), params)
    return row


def host_beat(conn: Connection, name: str, versions: dict[str, Any], now: str) -> dict[str, Any] | None:
    """Set beat_at and versions_json; state and updated_* stay as they were."""
    mark = conn.dialect.placeholder
    with conn.transaction():
        changed = conn.execute(
            f"UPDATE hosts SET beat_at = {mark}, versions_json = {mark} WHERE name = {mark}", [now, json_text(versions), name]
        )
        return _find(conn, name) if changed else None


def host_set_state(conn: Connection, name: str, state: str, by: str, now: str) -> dict[str, Any] | None:
    """Rewrite state, updated_at and updated_by only."""
    mark = conn.dialect.placeholder
    with conn.transaction():
        changed = conn.execute(
            f"UPDATE hosts SET state = {mark}, updated_at = {mark}, updated_by = {mark} WHERE name = {mark}",
            [state, now, by, name],
        )
        return _find(conn, name) if changed else None


def host_list(conn: Connection) -> list[dict[str, Any]]:
    return [_row(r) for r in conn.query_all(f"SELECT {', '.join(HOST_COLUMNS)} FROM hosts ORDER BY name")]
