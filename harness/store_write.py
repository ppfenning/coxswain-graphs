"""Turns harness records into run-record table rows, and writes them once.

The builders are pure: plain data in, plain row dicts out, no clock and no I/O. JSON
columns hold Python values in a row; `Store` encodes them with `json_text` at the edge.
Booleans are 0/1, and anything a record does not carry is None.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from harness.store_dialect import Connection, insert_ignore, json_text

Row = dict[str, Any]

# Table to primary key, as harness/store_ddl_0001.py declares them.
_KEYS: dict[str, tuple[str, ...]] = {
    "runs": ("run_id",),
    "phases": ("run_id", "phase_id"),
    "tasks": ("run_id", "task_id"),
    "attempts": ("run_id", "task_id", "seq"),
    "node_calls": ("call_id",),
    "ledger": ("row_hash",),
    "gate_decisions": ("run_id", "phase_id", "seq"),
    "leases": ("name",),
}

_RUN_KEYS = ("principal", "cartridge_sha", "cartridge_team", "overlay_sha", "provider_profile")
_CALL_KEYS = (
    "task_id",
    "role",
    "tier",
    "cost_usd",
    "ceiling_usd",
    "ceiling_source",
    "turns",
    "duration_ms",
    "input_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "input_total",
    "output_tokens",
    "ts",
)
_CALL_RENAMED = ("id", "model", "ok")
_DECISION_KEYS = (
    "requested_tier",
    "chosen_tier",
    "model_id",
    "claude_code_version",
    "router_tier",
    "router_reason",
    "ticket_key",
    "outcome_key",
    "system_one_confidence",
)
_LEDGER_KEYS = (
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
_GATE_KEYS = ("kind", "target", "decision", "risk", "outcome")
_GATE_FLAGS = ("applied", "edited")


def _pick(src: Mapping[str, Any], keys: Sequence[str]) -> Row:
    return {k: src.get(k) for k in keys}


def _flag(value: bool | None) -> int | None:
    return None if value is None else int(value)


def _rest(src: Mapping[str, Any], known: Sequence[str]) -> Row | None:
    """The keys of `src` outside `known`, or None when there are none."""
    extra = {k: v for k, v in src.items() if k not in known}
    return extra or None


def split_phase_id(run_id: str) -> tuple[str, str]:
    """Split 'run:phase' at the first colon. A run-level id has phase '', never None: phase_id is a key column."""
    run, _, phase = run_id.partition(":")
    return run, phase


def run_row(record: Mapping[str, Any], launch: Mapping[str, Any] | None = None) -> Row:
    launched = launch or {}
    return {
        "run_id": record["run_id"],
        **_pick(record, _RUN_KEYS),
        "launched_by": launched.get("launched_by"),
        "launched_at": launched.get("at"),
        "graph_id": launched.get("graph_id"),
        "started_at": None,
        "ended_at": None,
        "status": None,
        "record_json": dict(record),
    }


def phase_row(record: Mapping[str, Any]) -> Row:
    run_id, phase_id = split_phase_id(record["run_id"])
    return {
        "run_id": run_id,
        "phase_id": phase_id,
        **_pick(record, ("ts", "principal", "human_minutes")),
        "totals_json": record.get("totals"),
        "record_json": dict(record),
    }


def task_row(run_id: str, phase_id: str, task_id: str, state: str, updated_at: str) -> Row:
    return {"run_id": run_id, "phase_id": phase_id, "task_id": task_id, "state": state, "updated_at": updated_at}


def attempt_row(run_id: str, task_id: str, seq: int, phase_id: str, kind: str, reason: str | None, ts: str) -> Row:
    return {
        "run_id": run_id,
        "task_id": task_id,
        "seq": seq,
        "phase_id": phase_id,
        "kind": kind,
        "reason": reason,
        "ts": ts,
    }


def call_row(
    call: Mapping[str, Any],
    decision: Mapping[str, Any] | None = None,
    *,
    run_id: str,
    seq: int,
    phase_id: str | None = None,
) -> Row:
    """One node_calls row. `decision` has the shape of CallDecision.to_row and is kept whole in decision_json.

    model_alias is the call's own `model`; model_id is the decision's. A decision's `reason` is
    decision_reason and its `system_one_answer` is system_one_prediction. Call keys with no
    column of their own go to detail_json.
    """
    chosen = decision or {}
    return {
        "call_id": call.get("id"),
        "run_id": run_id,
        "seq": seq,
        "phase_id": phase_id,
        **_pick(call, _CALL_KEYS),
        "model_alias": call.get("model"),
        "ok": _flag(call.get("ok")),
        **_pick(chosen, _DECISION_KEYS),
        "decision_reason": chosen.get("reason"),
        "system_one_prediction": chosen.get("system_one_answer"),
        "decision_json": dict(chosen) if decision is not None else None,
        "detail_json": _rest(call, (*_CALL_KEYS, *_CALL_RENAMED)),
    }


def ledger_row(entry: Mapping[str, Any], *, epoch: int | None = None) -> Row:
    """row_hash is sha256 of the canonical row without row_hash and epoch, so a rerun under a new epoch is the same row."""
    body = {**_pick(entry, _LEDGER_KEYS), "row_json": dict(entry)}
    return {**body, "row_hash": hashlib.sha256(json_text(body).encode()).hexdigest(), "epoch": epoch}


def gate_rows(
    run_id: str, phase_id: str, decisions: Sequence[Mapping[str, Any]], *, epoch: int | None = None
) -> list[Row]:
    """One row per decision, seq being its index. A run-level list passes phase_id ''."""
    return [
        {
            "run_id": run_id,
            "phase_id": phase_id,
            "seq": i,
            **_pick(d, _GATE_KEYS),
            **{k: _flag(d.get(k)) for k in _GATE_FLAGS},
            "epoch": epoch,
            "detail_json": _rest(d, (*_GATE_KEYS, *_GATE_FLAGS)),
        }
        for i, d in enumerate(decisions)
    ]


@dataclass(frozen=True)
class Store:
    """The edge: inserts built rows, skipping any whose primary key is already present.

    Each `record_*` returns the rows actually inserted, 0 on a rerun. `epoch` is stored on
    ledger and gate rows; the other tables have no epoch column, so it is accepted and unused there.
    """

    conn: Connection

    def _insert(self, table: str, row: Row) -> int:
        columns = list(row)
        sql = insert_ignore(self.conn.dialect, table, columns, _KEYS[table])
        params = [json_text(v) if c.endswith("_json") and v is not None else v for c, v in row.items()]
        return self.conn.execute(sql, params)

    def record_run(
        self, record: Mapping[str, Any], launch: Mapping[str, Any] | None = None, epoch: int | None = None
    ) -> int:
        return self._insert("runs", run_row(record, launch))

    def finish_run(self, run_id: str, ended_at: str, status: str) -> int:
        """Stamp how a run ended. Returns the rows changed; the same values twice is harmless."""
        mark = self.conn.dialect.placeholder
        sql = f"UPDATE runs SET ended_at = {mark}, status = {mark} WHERE run_id = {mark}"
        return self.conn.execute(sql, (ended_at, status, run_id))

    def record_phase(self, record: Mapping[str, Any], epoch: int | None = None) -> int:
        return self._insert("phases", phase_row(record))

    def record_task(
        self, run_id: str, phase_id: str, task_id: str, state: str, updated_at: str, epoch: int | None = None
    ) -> int:
        return self._insert("tasks", task_row(run_id, phase_id, task_id, state, updated_at))

    def record_attempt(
        self,
        run_id: str,
        task_id: str,
        seq: int,
        phase_id: str,
        kind: str,
        reason: str | None,
        ts: str,
        epoch: int | None = None,
    ) -> int:
        return self._insert("attempts", attempt_row(run_id, task_id, seq, phase_id, kind, reason, ts))

    def record_call(
        self,
        call: Mapping[str, Any],
        decision: Mapping[str, Any] | None = None,
        *,
        run_id: str,
        seq: int,
        phase_id: str | None = None,
        epoch: int | None = None,
    ) -> int:
        return self._insert("node_calls", call_row(call, decision, run_id=run_id, seq=seq, phase_id=phase_id))

    def record_ledger(self, entry: Mapping[str, Any], epoch: int | None = None) -> int:
        return self._insert("ledger", ledger_row(entry, epoch=epoch))

    def record_gate_decisions(
        self, run_id: str, phase_id: str, decisions: Sequence[Mapping[str, Any]], epoch: int | None = None
    ) -> int:
        with self.conn.transaction():
            return sum(self._insert("gate_decisions", r) for r in gate_rows(run_id, phase_id, decisions, epoch=epoch))

    def total_rows(self, table: str) -> int:
        """Row count of a run-record table, for asserting that a write landed."""
        if table not in _KEYS:
            raise ValueError(f"unknown table {table!r}: expected one of {sorted(_KEYS)}")
        row = self.conn.query_one(f"SELECT COUNT(*) FROM {table}")
        return 0 if row is None else int(row[0])
