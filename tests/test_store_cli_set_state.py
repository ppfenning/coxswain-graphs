import json
import os
import uuid

import pytest
from conftest import T0, with_search_path

import harness.store_cli as store_cli
from harness.store_cli import main
from harness.store_migrate import open_store
from harness.store_read import work_items
from harness.store_work_state import set_state
from harness.store_write import upsert_work_item

NOW = "2026-09-25T01:02:03Z"
KEYS = {"initiative", "task_id", "phase", "state", "needs", "updated_at", "updated_by"}


@pytest.fixture(params=["sqlite", "postgres"])
def url(request, tmp_path):
    """A store URL a second connection can reach: a sqlite file, or a Postgres schema of its own."""
    if request.param == "sqlite":
        yield f"sqlite:///{tmp_path / 'cox.db'}"
        return
    base = os.environ.get("COXSWAIN_TEST_PG_URL")
    if not base:
        pytest.skip("COXSWAIN_TEST_PG_URL is not set")
    import psycopg

    schema = f"t_{uuid.uuid4().hex}"
    admin = psycopg.connect(base, autocommit=True)
    try:
        admin.execute(f"CREATE SCHEMA {schema}")
        yield with_search_path(base, schema)
    finally:
        try:
            admin.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        finally:
            admin.close()


@pytest.fixture
def run(capsys, tmp_path, monkeypatch):
    """Call main with the clock pinned to NOW; the code, the stdout and the stderr."""
    monkeypatch.setattr(store_cli, "_now", lambda: NOW)

    def call(store_url, *argv):
        code = main(["--store-url", store_url, "--runs-dir", str(tmp_path), "--provider-profile", str(tmp_path / "none.yaml"), *argv])
        out, err = capsys.readouterr()
        return code, out, err

    return call


def rows(url, initiative):
    conn = open_store(url, T0)
    try:
        return work_items(conn, initiative)
    finally:
        conn.close()


def seed(url, phase="p1", state="ready", needs=("t0",)):
    conn = open_store(url, T0)
    try:
        upsert_work_item(conn, "i1", "t1", phase, state, list(needs), T0, "alice")
    finally:
        conn.close()


def one_object(out):
    assert out.endswith("\n") and out.count("\n") == 1
    return json.loads(out)


def test_handler_keeps_phase_and_needs_of_an_existing_row(store_conn):
    upsert_work_item(store_conn, "i1", "t1", "p1", "ready", ["t0"], T0, "alice")
    assert set_state(store_conn, "i1", "t1", "done", "bob", "ignored", NOW) == {
        "initiative": "i1",
        "task_id": "t1",
        "phase": "p1",
        "state": "done",
        "needs": ["t0"],
        "updated_at": NOW,
        "updated_by": "bob",
    }
    assert work_items(store_conn, "i1")[0]["phase"] == "p1"


def test_handler_inserts_a_missing_row_with_empty_needs(store_conn):
    assert set_state(store_conn, "i1", "t1", "approved", "bob", "p2", NOW) == {
        "initiative": "i1",
        "task_id": "t1",
        "phase": "p2",
        "state": "approved",
        "needs": [],
        "updated_at": NOW,
        "updated_by": "bob",
    }
    assert len(work_items(store_conn, "i1")) == 1


def test_handler_returns_none_and_writes_nothing_without_a_row_or_phase(store_conn):
    assert set_state(store_conn, "i1", "t1", "done", "bob", None, NOW) is None
    assert work_items(store_conn, "i1") == []


def test_handler_leaves_other_tasks_alone(store_conn):
    upsert_work_item(store_conn, "i1", "t2", "p1", "ready", [], T0, "alice")
    assert set_state(store_conn, "i1", "t1", "done", "bob", None, NOW) is None
    assert [r["task_id"] for r in work_items(store_conn, "i1")] == ["t2"]


def test_exit_zero_prints_the_row_with_exactly_the_work_item_keys(url, run):
    seed(url)
    code, out, err = run(url, "set-state", "i1", "t1", "done", "--by", "bob")
    assert (code, err) == (0, "")
    row = one_object(out)
    assert set(row) == KEYS
    assert row == {
        "initiative": "i1",
        "task_id": "t1",
        "phase": "p1",
        "state": "done",
        "needs": ["t0"],
        "updated_at": NOW,
        "updated_by": "bob",
    }
    assert rows(url, "i1") == [row]


def test_exit_zero_inserts_when_a_phase_is_given(url, run):
    code, out, err = run(url, "set-state", "i1", "t1", "ready", "--by", "bob", "--phase", "p9")
    assert (code, err) == (0, "")
    assert one_object(out)["needs"] == []
    assert [(r["phase"], r["state"]) for r in rows(url, "i1")] == [("p9", "ready")]


def test_exit_three_when_no_row_exists_and_no_phase_was_given(url, run):
    code, out, err = run(url, "set-state", "i1", "t1", "done", "--by", "bob")
    assert code == 3
    assert one_object(out) == {}
    assert err.startswith("error: no work item t1 in initiative i1")
    assert rows(url, "i1") == []


@pytest.mark.parametrize(
    "argv",
    [
        ("set-state", "i1", "t1", "planned", "--by", "bob"),
        ("set-state", "i1", "t1", "done", "--by", ""),
        ("set-state", "i1", "t1", "done", "--by", "  "),
        ("set-state", "i1", "t1", "done"),
    ],
)
def test_exit_two_on_bad_input(url, run, argv):
    code, out, err = run(url, *argv)
    assert (code, out) == (2, "")
    assert err
    assert rows(url, "i1") == []


def test_exit_two_when_the_store_cannot_be_opened(run, tmp_path):
    code, out, err = run(f"sqlite:///{tmp_path / 'no' / 'such' / 'cox.db'}", "set-state", "i1", "t1", "done", "--by", "bob", "--phase", "p1")
    assert (code, out) == (2, "")
    assert err.startswith("error: cannot open the store")


def test_a_repeat_call_leaves_one_row(url, run):
    argv = ("set-state", "i1", "t1", "approved", "--by", "bob", "--phase", "p1")
    first = run(url, *argv)
    second = run(url, *argv)
    assert first[0] == second[0] == 0
    assert first[1] == second[1]
    assert len(rows(url, "i1")) == 1
