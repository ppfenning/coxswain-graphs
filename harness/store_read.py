"""The read path the tools repository uses. It only reads: no write and no migration.

Standard library only, plus store_dialect, store_migrate.check_version and
store_traces. It must not import `graphs`, which is optional for tools, and the
postgres driver stays a lazy import inside store_dialect.connect. Tests import
this module in a fresh interpreter with the harness package stubbed out, and
read its source for every import, lazy ones included.

What connect_readonly guarantees. It never creates a database file and never
changes the bytes of an existing one. On sqlite the connection is opened with
mode=ro, so a write is refused by the engine. Reading a WAL store, which every
store made by open_store is, can leave `-shm` and `-wal` files beside the
database, because SQLite needs them to read. In a directory the process cannot
write, with no `-wal` beside the file, mode=ro fails, so the file is opened
immutable=1 instead and nothing is created. On postgres the session is set
read-only before any query. That statement has not been run against a server.

Every function takes a Connection and returns plain dicts or lists of dicts.
Queries are portable SQL: parameters use the connection's placeholder and no
construct that `forbidden_constructs` bans appears in them.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from harness import store_traces
from harness.store_dialect import SQLITE, Connection, connect, json_load
from harness.store_migrate import check_version

__all__ = [
    "StoreVersionError",
    "attempts",
    "calls",
    "connect_readonly",
    "cost_by_model",
    "current_lease",
    "gate_decisions",
    "ledger_rows",
    "list_runs",
    "model_row",
    "read_trace",
    "run_graph",
    "run_summary",
    "summary_row",
    "task_record",
    "task_records",
    "work_items",
]

Row = dict[str, Any]

_RUN_COLS = ("run_id", "principal", "status", "started_at", "ended_at", "graph_id")
_CALL_COLS = (
    "call_id", "run_id", "seq", "phase_id", "task_id", "node_id", "role", "tier", "model_alias", "model_id",
    "claude_code_version", "cost_usd", "ceiling_usd", "ceiling_source", "turns", "duration_ms", "input_tokens",
    "cache_read_tokens", "cache_creation_tokens", "input_total", "output_tokens", "ok", "ts", "requested_tier",
    "chosen_tier", "decision_reason", "router_tier", "router_reason", "ticket_key", "outcome_key",
    "system_one_prediction", "system_one_confidence", "decision_json", "detail_json",
)  # fmt: skip
_ATTEMPT_COLS = ("run_id", "task_id", "seq", "phase_id", "kind", "reason", "ts")
_LEDGER_COLS = (
    "row_hash", "run_id", "ts", "principal", "kind", "risk", "outcome", "cartridge_sha", "provider_profile",
    "schema_tag", "epoch", "row_json",
)  # fmt: skip
_GATE_COLS = ("run_id", "phase_id", "seq", "kind", "target", "decision", "risk", "outcome", "applied", "edited", "epoch", "detail_json")
_LEASE_COLS = ("name", "holder", "epoch", "heartbeat_at", "expires_at")
_GRAPH_COLS = ("graph_id", "name", "version", "content_hash", "registered_at", "definition_json")
_NODE_COLS = ("graph_id", "node_id", "ord", "role", "default_tier", "default_class", "output_schema_hash")
_TASK_RECORD_COLS = ("run_id", "phase_id", "task_id", "record_json", "updated_at")
_WORK_ITEM_COLS = ("initiative", "task_id", "phase", "state", "needs_json", "updated_at", "updated_by")
_JSON_COLS = frozenset({"decision_json", "detail_json", "row_json", "definition_json", "record_json", "needs_json"})


class StoreVersionError(RuntimeError):
    """The database is not at the schema version this code expects."""


def _dict(cols: Sequence[str], row: Sequence[Any]) -> Row:
    """Column names zipped to a row; json columns decoded, None kept as None."""
    return {c: json_load(v) if c in _JSON_COLS else v for c, v in zip(cols, row)}


def _select(cols: Sequence[str], table: str) -> str:
    return f"SELECT {', '.join(cols)} FROM {table}"


def _sqlite_file(url: str) -> str | None:
    """The file a sqlite URL names; None for a non-sqlite or in-memory URL.

    Parses as store_dialect.connect does. A test compares the two, because a private
    import from store_dialect would break tools at import time on a rename.
    """
    if not url.startswith("sqlite://"):
        return None
    rest = url[len("sqlite://") :]
    if rest in ("", "/:memory:"):
        return None
    if rest.startswith("/") and len(rest) > 1:
        return rest[1:]
    raise ValueError(f"unreadable sqlite URL {url!r}: use sqlite:///<path> or sqlite:///:memory:")


def _sqlite_uri(path: Path) -> str:
    """mode=ro; also immutable=1 when the directory is unwritable and no -wal exists.

    Measured on SQLite 3.53: mode=ro alone fails there with "attempt to write a readonly
    database", because it cannot create -shm. No -wal means no connection has the store
    open, so an immutable read sees all committed data. A writer that opens the store
    after this read starts can make an immutable connection see stale pages.
    """
    quiet = not os.access(path.parent, os.W_OK) and not path.with_name(f"{path.name}-wal").exists()
    return f"{path.resolve().as_uri()}?mode=ro" + ("&immutable=1" if quiet else "")


def _open(url: str) -> Connection:
    """A sqlite file opens read-only with no pragmas; store_dialect.connect would create a missing file."""
    target = _sqlite_file(url)
    if target is None:
        return connect(url)
    path = Path(target)
    if not path.is_file():
        raise FileNotFoundError(f"no store at {target}: connect_readonly never creates one")
    return Connection(sqlite3.connect(_sqlite_uri(path), uri=True, timeout=30.0, isolation_level=None), SQLITE)


def connect_readonly(url: str) -> Connection:
    """Connect without writing and check the schema version; a mismatch closes the connection and raises."""
    conn = _open(url)
    try:
        if conn.dialect.name == "postgres":
            conn.execute("SET default_transaction_read_only = on")
        current, expected = check_version(conn)
    except BaseException:
        conn.close()
        raise
    if current != expected:
        conn.close()
        word = "newer" if current > expected else "older"
        raise StoreVersionError(f"store is at schema version {current}, {word} than the {expected} this code expects")
    return conn


def list_runs(conn: Connection, since: str, limit: int) -> list[Row]:
    """Runs started at or after `since` (ISO-8601 text), newest first, at most `limit`."""
    p = conn.dialect.placeholder
    sql = f"{_select(_RUN_COLS, 'runs')} WHERE started_at >= {p} ORDER BY started_at DESC, run_id LIMIT {p}"
    return [_dict(_RUN_COLS, r) for r in conn.query_all(sql, (since, limit))]


def _cache_share(cache_read_tokens: int, input_total: int) -> float | None:
    return None if input_total == 0 else cache_read_tokens / input_total


def summary_row(run_id: str, row: Sequence[Any]) -> Row:
    """Aggregate tuple to plain int and float. Postgres SUM gives Decimal, so it is coerced here."""
    n, cost, turns, input_total, cache_read, output = row
    return {
        "run_id": run_id,
        "calls": int(n),
        "cost_usd": float(cost),
        "turns": int(turns),
        "input_total": int(input_total),
        "cache_read_tokens": int(cache_read),
        "output_tokens": int(output),
        "cache_share": _cache_share(int(cache_read), int(input_total)),
    }


def run_summary(conn: Connection, run_id: str) -> Row | None:
    """Totals over a run's node_calls; None when the run is not in runs. cache_share is None when input_total is 0."""
    p = conn.dialect.placeholder
    sql = (
        "SELECT COUNT(c.call_id), COALESCE(SUM(c.cost_usd), 0), COALESCE(SUM(c.turns), 0),"
        " COALESCE(SUM(c.input_total), 0), COALESCE(SUM(c.cache_read_tokens), 0), COALESCE(SUM(c.output_tokens), 0)"
        f" FROM runs r LEFT JOIN node_calls c ON c.run_id = r.run_id WHERE r.run_id = {p} GROUP BY r.run_id"
    )
    row = conn.query_one(sql, (run_id,))
    return None if row is None else summary_row(run_id, row)


def model_row(row: Sequence[Any]) -> Row:
    """One cost_by_model group as plain str, int and float."""
    alias, tier, n, cost, input_total, output = row
    return {
        "model_alias": alias,
        "tier": tier,
        "calls": int(n),
        "cost_usd": float(cost),
        "input_total": int(input_total),
        "output_tokens": int(output),
    }


def cost_by_model(conn: Connection, run_id: str) -> list[Row]:
    """One row per (model_alias, tier) with call count, cost and token totals."""
    p = conn.dialect.placeholder
    sql = (
        "SELECT model_alias, tier, COUNT(*), COALESCE(SUM(cost_usd), 0), COALESCE(SUM(input_total), 0),"
        " COALESCE(SUM(output_tokens), 0)"
        f" FROM node_calls WHERE run_id = {p} GROUP BY model_alias, tier ORDER BY model_alias, tier"
    )
    return [model_row(r) for r in conn.query_all(sql, (run_id,))]


def calls(conn: Connection, run_id: str, role: str | None = None) -> list[Row]:
    """A run's node_calls in seq order; only those of `role` when it is given."""
    p = conn.dialect.placeholder
    base = f"{_select(_CALL_COLS, 'node_calls')} WHERE run_id = {p}"
    sql, params = (
        (f"{base} ORDER BY seq", (run_id,)) if role is None else (f"{base} AND role = {p} ORDER BY seq", (run_id, role))
    )
    return [_dict(_CALL_COLS, r) for r in conn.query_all(sql, params)]


def attempts(conn: Connection, task_id: str) -> list[Row]:
    """Every attempt recorded for a task, by run then seq."""
    p = conn.dialect.placeholder
    sql = f"{_select(_ATTEMPT_COLS, 'attempts')} WHERE task_id = {p} ORDER BY run_id, seq"
    return [_dict(_ATTEMPT_COLS, r) for r in conn.query_all(sql, (task_id,))]


def ledger_rows(conn: Connection, run_id: str) -> list[Row]:
    """A run's ledger rows in epoch, ts order, row_json decoded."""
    p = conn.dialect.placeholder
    sql = f"{_select(_LEDGER_COLS, 'ledger')} WHERE run_id = {p} ORDER BY epoch, ts, row_hash"
    return [_dict(_LEDGER_COLS, r) for r in conn.query_all(sql, (run_id,))]


def gate_decisions(conn: Connection, run_id: str) -> list[Row]:
    """A run's gate decisions by phase then seq, detail_json decoded."""
    p = conn.dialect.placeholder
    sql = f"{_select(_GATE_COLS, 'gate_decisions')} WHERE run_id = {p} ORDER BY phase_id, seq"
    return [_dict(_GATE_COLS, r) for r in conn.query_all(sql, (run_id,))]


def current_lease(conn: Connection, name: str) -> Row | None:
    """The lease row for `name`, or None when nobody holds it."""
    p = conn.dialect.placeholder
    row = conn.query_one(f"{_select(_LEASE_COLS, 'leases')} WHERE name = {p}", (name,))
    return None if row is None else _dict(_LEASE_COLS, row)


def run_graph(conn: Connection, run_id: str) -> Row | None:
    """The graph a run used: its graphs row plus `nodes` in ord order and `edges`.

    None when the run has no graph_id or the graph was never registered. The join is by
    value, because migration 0002 declares no foreign key.
    """
    p = conn.dialect.placeholder
    graph = conn.query_one(
        f"{_select(_GRAPH_COLS, 'graphs')} WHERE graph_id = (SELECT graph_id FROM runs WHERE run_id = {p})",
        (run_id,),
    )
    if graph is None:
        return None
    head = _dict(_GRAPH_COLS, graph)
    gid = head["graph_id"]
    nodes = conn.query_all(f"{_select(_NODE_COLS, 'graph_nodes')} WHERE graph_id = {p} ORDER BY ord, node_id", (gid,))
    edges = conn.query_all(f"SELECT src, dst FROM graph_edges WHERE graph_id = {p} ORDER BY src, dst", (gid,))
    return {
        **head,
        "nodes": [_dict(_NODE_COLS, n) for n in nodes],
        "edges": [_dict(("src", "dst"), e) for e in edges],
    }


def task_record(conn: Connection, run_id: str, phase_id: str, task_id: str) -> Row | None:
    """The decoded record_json of one task, or None when the store has no such row."""
    p = conn.dialect.placeholder
    sql = f"{_select(_TASK_RECORD_COLS, 'task_records')} WHERE run_id = {p} AND phase_id = {p} AND task_id = {p}"
    row = conn.query_one(sql, (run_id, phase_id, task_id))
    return None if row is None else _dict(_TASK_RECORD_COLS, row)["record_json"]


def task_records(conn: Connection, run_id: str) -> dict[tuple[str, str], Row]:
    """A run's task records keyed by (phase_id, task_id), in that key order."""
    p = conn.dialect.placeholder
    sql = f"{_select(_TASK_RECORD_COLS, 'task_records')} WHERE run_id = {p} ORDER BY phase_id, task_id"
    rows = (_dict(_TASK_RECORD_COLS, r) for r in conn.query_all(sql, (run_id,)))
    return {(r["phase_id"], r["task_id"]): r["record_json"] for r in rows}


def work_items(conn: Connection, initiative: str) -> list[Row]:
    """An initiative's work_items rows in task_id order, needs_json decoded and keyed as `needs`."""
    p = conn.dialect.placeholder
    sql = f"{_select(_WORK_ITEM_COLS, 'work_items')} WHERE initiative = {p} ORDER BY task_id"
    rows = (_dict(_WORK_ITEM_COLS, r) for r in conn.query_all(sql, (initiative,)))
    return [{("needs" if k == "needs_json" else k): v for k, v in r.items()} for r in rows]


def read_trace(root: str | Path, run_id: str, call_id: str) -> list[Row]:
    """A call's trace events in seq order; empty when there is none."""
    return store_traces.read_call(Path(root), run_id, call_id)
