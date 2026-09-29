import pytest

from harness.store_pause import clear_paused, is_paused, set_paused

RUN_ID = "r1"
MISSING = "no-such-run"
AT = "2026-09-29T04:52:00Z"


@pytest.fixture
def conn(store_conn):
    store_conn.execute("INSERT INTO runs (run_id, status) VALUES ('r1', 'queued')")
    return store_conn


def test_is_paused_false_before_any_call(conn):
    assert is_paused(conn, RUN_ID) is False


def test_is_paused_true_after_set_paused(conn):
    set_paused(conn, RUN_ID, AT)
    assert is_paused(conn, RUN_ID) is True


def test_is_paused_false_again_after_clear_paused(conn):
    set_paused(conn, RUN_ID, AT)
    clear_paused(conn, RUN_ID)
    assert is_paused(conn, RUN_ID) is False


def test_set_paused_on_missing_run_id_is_a_no_op(conn):
    before = conn.query_all("SELECT run_id FROM runs")
    set_paused(conn, MISSING, AT)
    assert is_paused(conn, MISSING) is False
    assert conn.query_all("SELECT run_id FROM runs") == before
