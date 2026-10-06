import logging
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from harness.store_dialect import json_load
from harness.store_write import Store, merge_outcome

RUN = {"run_id": "r1", "ts": "2026-10-06T00:00:00Z", "principal": "epic-swarm"}
OUTCOME = {"status": "ok", "cost_usd": 1.5}


def test_merge_keeps_sibling_keys():
    assert merge_outcome({"a": 1, "b": [2]}, OUTCOME) == {"a": 1, "b": [2], "outcome": OUTCOME}


def test_merge_replaces_a_prior_outcome():
    assert merge_outcome({"a": 1, "outcome": {"status": "old"}}, OUTCOME) == {"a": 1, "outcome": OUTCOME}


@pytest.mark.parametrize("record", [None, {}])
def test_merge_starts_from_an_empty_object_for_a_null_or_empty_record(record):
    assert merge_outcome(record, OUTCOME) == {"outcome": OUTCOME}


def test_merge_returns_a_new_dict_and_leaves_its_inputs_alone():
    record, outcome = {"a": 1}, {"status": "ok"}
    merged = merge_outcome(record, outcome)
    merged["outcome"]["status"] = "changed"
    assert (record, outcome) == ({"a": 1}, {"status": "ok"})


def stored(conn, run_id="r1"):
    mark = conn.dialect.placeholder
    return json_load(conn.query_one(f"SELECT record_json FROM runs WHERE run_id = {mark}", (run_id,))[0])


def test_record_outcome_merges_into_a_stored_run(conn_store):
    conn, store = conn_store
    store.record_run(RUN)
    assert store.record_outcome("r1", OUTCOME) is True
    assert stored(conn) == {**RUN, "outcome": OUTCOME}
    assert store.record_outcome("r1", {"status": "failed"}) is True
    assert stored(conn) == {**RUN, "outcome": {"status": "failed"}}


def test_record_outcome_on_a_null_record_json(conn_store):
    conn, store = conn_store
    store.record_run(RUN)
    mark = conn.dialect.placeholder
    conn.execute(f"UPDATE runs SET record_json = NULL WHERE run_id = {mark}", ("r1",))
    assert store.record_outcome("r1", OUTCOME) is True
    assert stored(conn) == {"outcome": OUTCOME}


def test_record_outcome_for_a_missing_run_returns_false_and_logs(conn_store, caplog):
    _, store = conn_store
    with caplog.at_level(logging.ERROR):
        assert store.record_outcome("nope", OUTCOME) is False
    assert "nope" in caplog.text


def test_record_outcome_returns_false_and_logs_when_the_store_raises(caplog):
    def boom(*_args, **_kwargs):
        raise RuntimeError("store down")

    conn = SimpleNamespace(
        dialect=SimpleNamespace(placeholder="?"), transaction=nullcontext, query_one=boom, execute=boom
    )
    with caplog.at_level(logging.ERROR):
        assert Store(conn).record_outcome("r1", OUTCOME) is False
    assert "r1" in caplog.text and "store down" in caplog.text


@pytest.fixture
def conn_store(store_conn):
    return store_conn, Store(store_conn)
