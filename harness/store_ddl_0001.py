"""Migration 0001: run records, the ledger, gate decisions and the lease table.

Portable types only. Timestamps are ISO-8601 TEXT, booleans are SMALLINT 0/1 and
variable payloads use the dialect's json type. Every table has a primary key so a
writer can insert-ignore. Cache share is derived at read time, never stored.
"""

from __future__ import annotations

from harness.store_dialect import Dialect

VERSION = 1
DESCRIPTION = "run records, ledger, gate decisions, leases"

# The JSON and BOOL tokens are resolved per dialect; the rest are literal SQL types.
_TEXT, _INT, _BIG, _REAL, _BOOL, _JSON = "TEXT", "INTEGER", "BIGINT", "REAL", "BOOL", "JSON"

Column = tuple[str, str]


def _cols(kind: str, *names: str) -> tuple[Column, ...]:
    return tuple((n, kind) for n in names)


_TABLES: tuple[tuple[str, tuple[Column, ...], tuple[str, ...]], ...] = (
    (
        "runs",
        _cols(
            _TEXT,
            "run_id",
            "principal",
            "launched_by",
            "launched_at",
            "cartridge_sha",
            "cartridge_team",
            "overlay_sha",
            "provider_profile",
            "started_at",
            "ended_at",
            "status",
        )
        + _cols(_JSON, "record_json"),
        ("run_id",),
    ),
    (
        "phases",
        _cols(_TEXT, "run_id", "phase_id", "ts", "principal")
        + _cols(_REAL, "human_minutes")
        + _cols(_JSON, "totals_json", "record_json"),
        ("run_id", "phase_id"),
    ),
    (
        "tasks",
        _cols(_TEXT, "run_id", "phase_id", "task_id", "state", "updated_at"),
        ("run_id", "task_id"),
    ),
    (
        "attempts",
        _cols(_TEXT, "run_id", "task_id") + _cols(_INT, "seq") + _cols(_TEXT, "phase_id", "kind", "reason", "ts"),
        ("run_id", "task_id", "seq"),
    ),
    (
        "node_calls",
        _cols(_TEXT, "call_id", "run_id")
        + _cols(_INT, "seq")
        + _cols(_TEXT, "phase_id", "task_id", "role", "tier", "model_alias", "model_id", "claude_code_version")
        + _cols(_REAL, "cost_usd", "ceiling_usd")
        + _cols(_TEXT, "ceiling_source")
        + _cols(
            _INT,
            "turns",
            "duration_ms",
            "input_tokens",
            "cache_read_tokens",
            "cache_creation_tokens",
            "input_total",
            "output_tokens",
        )
        + _cols(_BOOL, "ok")
        + _cols(
            _TEXT,
            "ts",
            "requested_tier",
            "chosen_tier",
            "decision_reason",
            "router_tier",
            "router_reason",
            "ticket_key",
            "outcome_key",
            "system_one_prediction",
        )
        + _cols(_REAL, "system_one_confidence")
        + _cols(_JSON, "decision_json", "detail_json"),
        ("call_id",),
    ),
    (
        "ledger",
        _cols(
            _TEXT,
            "row_hash",
            "run_id",
            "ts",
            "principal",
            "kind",
            "risk",
            "outcome",
            "cartridge_sha",
            "provider_profile",
            "schema_tag",
        )
        + _cols(_BIG, "epoch")
        + _cols(_JSON, "row_json"),
        ("row_hash",),
    ),
    (
        "gate_decisions",
        _cols(_TEXT, "run_id", "phase_id")
        + _cols(_INT, "seq")
        + _cols(_TEXT, "kind", "target", "decision", "risk", "outcome")
        + _cols(_BOOL, "applied", "edited")
        + _cols(_BIG, "epoch")
        + _cols(_JSON, "detail_json"),
        ("run_id", "phase_id", "seq"),
    ),
    (
        "leases",
        _cols(_TEXT, "name", "holder") + _cols(_BIG, "epoch") + _cols(_TEXT, "heartbeat_at", "expires_at"),
        ("name",),
    ),
)

_INDEXES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("ix_node_calls_run", "node_calls", ("run_id",)),
    ("ix_node_calls_role_tier", "node_calls", ("role", "tier")),
    ("ix_attempts_task", "attempts", ("task_id",)),
    ("ix_ledger_run", "ledger", ("run_id",)),
)


def _sql_type(dialect: Dialect, token: str) -> str:
    return {_JSON: dialect.json_type, _BOOL: dialect.bool_type}.get(token, token)


def _create_table(dialect: Dialect, name: str, columns: tuple[Column, ...], key: tuple[str, ...]) -> str:
    defs = ",\n".join(f"    {c} {_sql_type(dialect, t)}" for c, t in columns)
    return f"CREATE TABLE IF NOT EXISTS {name} (\n{defs},\n    PRIMARY KEY ({', '.join(key)})\n)"


def statements(dialect: Dialect) -> tuple[str, ...]:
    """The eight tables, then the four indexes."""
    return (
        *(_create_table(dialect, n, c, k) for n, c, k in _TABLES),
        *(f"CREATE INDEX IF NOT EXISTS {i} ON {t} ({', '.join(cs)})" for i, t, cs in _INDEXES),
    )
