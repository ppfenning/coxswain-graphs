from harness.store_dialect import json_load
from harness.store_write import Store, attempt_row, task_cause

KEY = ("r1", "p1", "t1")
TS = "2026-09-25T00:00:00Z"
CAUSED = {"cause": "flaky", "cause_why": "timed out twice"}


def stored(conn, seq=1):
    sql = f"SELECT cause, cause_why FROM attempts WHERE run_id = 'r1' AND task_id = 't1' AND seq = {seq}"
    return conn.query_one(sql)


def test_attempt_row_defaults_cause_to_none_and_carries_it_when_given():
    assert attempt_row("r", "t", 0, "p", "k", None, "ts")["cause"] is None
    row = attempt_row("r", "t", 0, "p", "k", None, "ts", **CAUSED)
    assert (row["cause"], row["cause_why"]) == ("flaky", "timed out twice")


def test_an_attempt_written_with_cause_reads_it_back(store_conn):
    assert Store(store_conn).record_attempt("r1", "t1", 1, "p1", "retry", None, TS, **CAUSED) == 1
    assert stored(store_conn) == ("flaky", "timed out twice")


def test_an_attempt_written_without_cause_reads_none(store_conn):
    assert Store(store_conn).record_attempt("r1", "t1", 1, "p1", "retry", None, TS) == 1
    assert stored(store_conn) == (None, None)


def test_cause_can_be_set_on_an_existing_attempt_and_leaves_others_alone(store_conn):
    store = Store(store_conn)
    store.record_attempt("r1", "t1", 1, "p1", "retry", None, TS)
    store.record_attempt("r1", "t1", 2, "p1", "retry", None, TS)
    assert store.set_attempt_cause("r1", "t1", 1, "flaky", "timed out twice") == 1
    assert stored(store_conn, 1) == ("flaky", "timed out twice")
    assert stored(store_conn, 2) == (None, None)
    assert store.set_attempt_cause("r1", "t1", 1, "bad-spec", None) == 1
    assert stored(store_conn, 1) == ("bad-spec", None)


def test_setting_cause_on_a_missing_attempt_changes_nothing(store_conn):
    store = Store(store_conn)
    assert store.set_attempt_cause("r1", "t1", 1, "flaky", "x") == 0
    assert store.total_rows("attempts") == 0


def test_a_task_record_with_cause_round_trips(store_conn):
    Store(store_conn).record_task_record(*KEY, {"state": "done", **CAUSED}, TS)
    raw = store_conn.query_one("SELECT record_json FROM task_records")[0]
    assert task_cause(json_load(raw)) == ("flaky", "timed out twice")


def test_a_task_record_without_cause_reads_none(store_conn):
    Store(store_conn).record_task_record(*KEY, {"state": "done"}, TS)
    raw = store_conn.query_one("SELECT record_json FROM task_records")[0]
    assert task_cause(json_load(raw)) == (None, None)
