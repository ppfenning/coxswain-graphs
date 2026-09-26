from pathlib import Path

import pytest

import harness.store_backfill as store_backfill
from harness.store_backfill import backfill, balanced, derive_cause
from harness.store_write import Store

TS = "2026-01-01T00:00:00+00:00"
# task id, kind, reason: one attempt per rule, then one that matches none.
SEEDS = (
    ("t-infra", "infra", "disk full"),
    ("t-patch", "refused", "patch did not apply: hunk 2"),
    ("t-tree", "refused", "worktree b could not be created: locked"),
    ("t-check", "unverified", "configured check failed: ruff"),
    ("t-nowork", "no_work", None),
    ("t-other", "refused", "something else"),
)
EXPECTED = {
    "t-infra": "harness",
    "t-patch": "harness",
    "t-tree": "harness",
    "t-check": "code",
    "t-nowork": "ticket",
    "t-other": "unknown",
}


@pytest.fixture
def paths(tmp_path):
    runs, work = tmp_path / "runs", tmp_path / "work"
    runs.mkdir()
    work.mkdir()
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("", encoding="utf-8")
    return runs, work, ledger


@pytest.fixture
def store(store_conn):
    s = Store(store_conn)
    for task, kind, reason in SEEDS:
        s.record_attempt("run-a", task, 0, "p1", kind, reason, TS)
    return s


def causes(conn):
    sql = "SELECT task_id, cause, cause_why FROM attempts ORDER BY task_id"
    return {t: (c, w) for t, c, w in conn.query_all(sql)}


def test_each_rule_and_the_unmatched_row_get_their_cause(store, paths):
    backfill(store, *paths)
    assert {t: c for t, (c, _) in causes(store.conn).items()} == EXPECTED


def test_the_report_counts_rows_filled_per_cause_and_still_balances(store, paths):
    report = backfill(store, *paths)
    filled = {k: v for k, v in report.items() if k.startswith("cause_filled_")}
    assert filled == {
        "cause_filled_ticket": 1,
        "cause_filled_code": 1,
        "cause_filled_review": 0,
        "cause_filled_harness": 3,
        "cause_filled_unknown": 1,
    }
    assert balanced(report)


def test_cause_why_stays_null_on_rule_derived_rows(store, paths):
    backfill(store, *paths)
    assert {w for _, w in causes(store.conn).values()} == {None}


def test_a_preset_cause_is_untouched_in_both_columns(store, paths):
    # The rule would say harness for this kind; a human set review.
    store.record_attempt("run-a", "t-human", 0, "p1", "infra", "x", TS, cause="review", cause_why="human read the diff")
    backfill(store, *paths)
    assert causes(store.conn)["t-human"] == ("review", "human read the diff")


def test_a_second_run_fills_zero_rows_and_changes_nothing(store, paths):
    backfill(store, *paths)
    before = causes(store.conn)
    report = backfill(store, *paths)
    assert {v for k, v in report.items() if k.startswith("cause_filled_")} == {0}
    assert causes(store.conn) == before


def test_derive_cause_reads_null_kind_and_reason_as_empty():
    assert derive_cause(None, None) == "unknown"
    assert derive_cause("no_work", None) == "ticket"


def test_the_backfill_module_does_not_touch_the_model_path():
    assert "cause_model" not in Path(store_backfill.__file__).read_text(encoding="utf-8")
