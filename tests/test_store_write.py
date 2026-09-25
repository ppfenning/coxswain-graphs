import hashlib

import pytest

from harness.store_dialect import POSTGRES, json_load, json_text
from harness.store_write import (
    Store,
    attempt_row,
    call_row,
    gate_rows,
    ledger_row,
    phase_row,
    run_row,
    split_phase_id,
    task_row,
)

PHASE = {
    "cartridge_sha": "bd20",
    "cartridge_team": "pat",
    "gate_diffs": [],
    "human_minutes": 0.0,
    "overlay_sha": None,
    "principal": "epic-swarm(lifecycle-propose)",
    "proposals": [],
    "provider_profile": "claude-code@f9b9789c88cc",
    "run_id": "graphs-model-router-2:p1-record",
    "totals": {"auto_applied": 0, "completed": 1, "gated": 0, "quarantined": 1, "ready": 1, "surviving": 0},
    "ts": "2026-09-24T14:01:59.877671+00:00",
}
RUN = {**PHASE, "run_id": "graphs-model-router-2"}
LAUNCH = {"launched_by": "chair-2026-09-23", "at": "2026-09-24T13:56:37.979501+00:00"}
CALL = {
    "role": "build",
    "task_id": "t1",
    "tier": "standard",
    "model": "sonnet",
    "tools": ["Read"],
    "cost_usd": 0.5,
    "ceiling_usd": 2.0,
    "ceiling_source": "cartridge",
    "turns": 3,
    "duration_ms": 1200,
    "input_tokens": 10,
    "cache_read_tokens": 20,
    "cache_creation_tokens": 5,
    "input_total": 35,
    "output_tokens": 7,
    "id": "call-1",
    "ts": "2026-09-24T14:00:00Z",
    "ok": True,
    "commands_run": ["pytest"],
}
DECISION = {
    "role": "build",
    "requested_tier": "standard",
    "chosen_tier": "fast",
    "model_id": "claude-x",
    "reason": "clipped",
    "ticket_key": "tk",
    "outcome_key": "ok",
    "router_tier": "cheap",
    "router_reason": "small change",
    "claude_code_version": "2.1.0",
    "system_one_answer": "yes",
    "system_one_confidence": 0.9,
}
GATE = {
    "applied": True,
    "decision": "approved",
    "edited": False,
    "kind": "item_create",
    "outcome": "clean",
    "risk": "low",
    "target": "T-1",
}


@pytest.fixture
def conn(store_conn):
    return store_conn


@pytest.fixture
def store(conn):
    return Store(conn)


def cols(conn, table):
    if conn.dialect is POSTGRES:
        sql = "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = %s"
        return {r[0] for r in conn.query_all(sql, (table,))}
    return {r[1] for r in conn.query_all(f"PRAGMA table_info({table})")}


def test_run_row_takes_the_launch_record_and_keeps_the_whole_record():
    assert run_row(RUN, LAUNCH) == {
        "run_id": "graphs-model-router-2",
        "principal": "epic-swarm(lifecycle-propose)",
        "cartridge_sha": "bd20",
        "cartridge_team": "pat",
        "overlay_sha": None,
        "provider_profile": "claude-code@f9b9789c88cc",
        "launched_by": "chair-2026-09-23",
        "launched_at": "2026-09-24T13:56:37.979501+00:00",
        "graph_id": None,
        "started_at": None,
        "ended_at": None,
        "status": None,
        "record_json": RUN,
    }
    assert run_row(RUN)["launched_by"] is None
    assert run_row(RUN, {**LAUNCH, "graph_id": "g1"})["graph_id"] == "g1"


def test_a_phase_records_run_id_splits_into_run_and_phase():
    assert split_phase_id("graphs-model-router-2:p1-record") == ("graphs-model-router-2", "p1-record")
    assert split_phase_id("graphs-model-router-2") == ("graphs-model-router-2", "")
    assert phase_row(PHASE) == {
        "run_id": "graphs-model-router-2",
        "phase_id": "p1-record",
        "ts": "2026-09-24T14:01:59.877671+00:00",
        "principal": "epic-swarm(lifecycle-propose)",
        "human_minutes": 0.0,
        "totals_json": PHASE["totals"],
        "record_json": PHASE,
    }


def test_task_and_attempt_rows_are_literal():
    assert task_row("r", "p", "t", "done", "ts0") == {
        "run_id": "r",
        "phase_id": "p",
        "task_id": "t",
        "state": "done",
        "updated_at": "ts0",
    }
    assert attempt_row("r", "t", 2, "p", "retry", None, "ts1") == {
        "run_id": "r",
        "task_id": "t",
        "seq": 2,
        "phase_id": "p",
        "kind": "retry",
        "reason": None,
        "ts": "ts1",
    }


def test_call_row_with_a_decision_promotes_its_fields():
    row = call_row(CALL, DECISION, run_id="r", seq=0, phase_id="p")
    assert row["call_id"] == "call-1"
    assert row["model_alias"] == "sonnet"
    assert row["model_id"] == "claude-x"
    assert row["ok"] == 1
    assert (row["router_tier"], row["router_reason"], row["claude_code_version"]) == ("cheap", "small change", "2.1.0")
    assert (row["requested_tier"], row["chosen_tier"], row["decision_reason"]) == ("standard", "fast", "clipped")
    assert (row["system_one_prediction"], row["system_one_confidence"]) == ("yes", 0.9)
    assert row["decision_json"] == DECISION
    assert row["detail_json"] == {"tools": ["Read"], "commands_run": ["pytest"]}


def test_call_row_without_a_decision_stores_none_for_every_decision_column():
    row = call_row(CALL, run_id="r", seq=0)
    assert row["decision_json"] is None
    assert row["phase_id"] is None
    for name in (
        "router_tier",
        "router_reason",
        "claude_code_version",
        "model_id",
        "decision_reason",
        "system_one_prediction",
    ):
        assert row[name] is None


def test_ledger_row_hash_is_stable_for_equal_rows_and_copies_the_epoch():
    a = ledger_row({"run_id": "r", "kind": "item_create", "risk": "low"}, epoch=4)
    b = ledger_row({"risk": "low", "kind": "item_create", "run_id": "r"}, epoch=9)
    assert a["row_hash"] == b["row_hash"]
    assert len(a["row_hash"]) == 64
    assert (a["epoch"], b["epoch"]) == (4, 9)
    assert ledger_row({"run_id": "r", "kind": "other"})["row_hash"] != a["row_hash"]
    assert ledger_row({"run_id": "r"})["epoch"] is None
    body = {k: a[k] for k in a if k not in ("row_hash", "epoch")}
    assert a["row_hash"] == hashlib.sha256(json_text(body).encode()).hexdigest()


def test_gate_rows_number_the_decisions_and_flatten_flags():
    assert gate_rows("r", "", [GATE, {**GATE, "note": "x"}], epoch=3) == [
        {
            "run_id": "r",
            "phase_id": "",
            "seq": 0,
            "kind": "item_create",
            "target": "T-1",
            "decision": "approved",
            "risk": "low",
            "outcome": "clean",
            "applied": 1,
            "edited": 0,
            "epoch": 3,
            "detail_json": None,
        },
        {
            "run_id": "r",
            "phase_id": "",
            "seq": 1,
            "kind": "item_create",
            "target": "T-1",
            "decision": "approved",
            "risk": "low",
            "outcome": "clean",
            "applied": 1,
            "edited": 0,
            "epoch": 3,
            "detail_json": {"note": "x"},
        },
    ]
    assert gate_rows("r", "p", []) == []


def test_every_builder_emits_exactly_the_tables_columns(conn):
    migrated = {"graph_id", "node_id"}
    assert set(run_row(RUN)) == cols(conn, "runs") - {"host"}  # host is written by a later ticket
    assert set(phase_row(PHASE)) == cols(conn, "phases")
    assert set(task_row("r", "p", "t", "s", "u")) == cols(conn, "tasks")
    assert set(attempt_row("r", "t", 0, "p", "k", None, "ts")) == cols(conn, "attempts")
    assert set(call_row(CALL, run_id="r", seq=0)) == cols(conn, "node_calls") - migrated
    assert set(ledger_row({})) == cols(conn, "ledger")
    assert set(gate_rows("r", "p", [GATE])[0]) == cols(conn, "gate_decisions")


def test_inserting_the_same_call_twice_gives_one_then_zero(store):
    assert store.record_call(CALL, DECISION, run_id="r", seq=0) == 1
    assert store.record_call(CALL, DECISION, run_id="r", seq=0) == 0
    assert store.total_rows("node_calls") == 1


def test_a_stored_call_fills_decision_columns_only_when_it_has_a_decision(store, conn):
    store.record_call(CALL, DECISION, run_id="r", seq=0)
    store.record_call({**CALL, "id": "call-2"}, run_id="r", seq=1)
    sql = (
        "SELECT router_tier, router_reason, claude_code_version, ok, decision_json "
        f"FROM node_calls WHERE call_id = {conn.dialect.placeholder}"
    )
    with_decision = conn.query_one(sql, ("call-1",))
    assert with_decision[:4] == ("cheap", "small change", "2.1.0", 1)
    assert json_load(with_decision[4]) == DECISION
    assert conn.query_one(sql, ("call-2",)) == (None, None, None, 1, None)


def test_a_phase_record_lands_split_and_a_rerun_writes_nothing(store, conn):
    assert store.record_phase(PHASE) == 1
    assert store.record_phase(PHASE) == 0
    run_id, phase_id, totals = conn.query_one("SELECT run_id, phase_id, totals_json FROM phases")
    assert (run_id, phase_id) == ("graphs-model-router-2", "p1-record")
    # Postgres hands JSONB back in its own spacing and key order, so compare decoded values, not text.
    assert json_load(totals) == PHASE["totals"]


def test_run_task_and_attempt_inserts_are_idempotent(store):
    assert [store.record_run(RUN, LAUNCH) for _ in range(2)] == [1, 0]
    assert [store.record_task("r", "p", "t", "done", "u") for _ in range(2)] == [1, 0]
    assert [store.record_attempt("r", "t", 0, "p", "first", None, "ts") for _ in range(2)] == [1, 0]
    assert store.record_attempt("r", "t", 1, "p", "retry", "again", "ts") == 1
    assert [store.total_rows(t) for t in ("runs", "tasks", "attempts")] == [1, 1, 2]


def test_finish_run_stamps_the_end_of_a_recorded_run_and_a_repeat_changes_nothing_new(store, conn):
    assert store.finish_run("graphs-model-router-2", "t0", "ok") == 0
    store.record_run(RUN, {**LAUNCH, "graph_id": "g1"})
    assert [store.finish_run("graphs-model-router-2", "t1", "failed") for _ in range(2)] == [1, 1]
    assert conn.query_one("SELECT graph_id, ended_at, status FROM runs") == ("g1", "t1", "failed")


def test_ledger_and_gate_rows_store_the_epoch_and_rerun_writes_nothing(store, conn):
    assert store.record_ledger({"run_id": "r", "kind": "k"}, epoch=7) == 1
    assert store.record_ledger({"run_id": "r", "kind": "k"}, epoch=8) == 0
    assert conn.query_one("SELECT epoch FROM ledger") == (7,)
    assert store.record_gate_decisions("r", "", [GATE, GATE], epoch=7) == 2
    assert store.record_gate_decisions("r", "", [GATE, GATE], epoch=7) == 0
    assert conn.query_all("SELECT seq, epoch, applied, edited FROM gate_decisions ORDER BY seq") == [
        (0, 7, 1, 0),
        (1, 7, 1, 0),
    ]


def test_total_rows_counts_and_rejects_an_unknown_table(store):
    assert store.total_rows("runs") == 0
    with pytest.raises(ValueError, match="unknown table"):
        store.total_rows("runs; DROP TABLE runs")
