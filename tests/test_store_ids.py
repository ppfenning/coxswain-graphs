import pytest

from harness.store_dialect import json_text
from harness.store_ids import allocate_initiative, backfill, resolve

pytestmark = pytest.mark.parametrize("store_url", ["sqlite"], indirect=True)


def _item(conn, initiative, task_id, phase, updated_at):
    conn.execute(
        "INSERT INTO work_items (initiative, task_id, phase, state, needs_json, updated_at, updated_by) "
        f"VALUES ({conn.dialect.placeholder}, {conn.dialect.placeholder}, {conn.dialect.placeholder}, "
        f"{conn.dialect.placeholder}, {conn.dialect.placeholder}, {conn.dialect.placeholder}, {conn.dialect.placeholder})",
        (initiative, task_id, phase, "ready", json_text([]), updated_at, "test"),
    )


def _run(conn, run_id, started_at):
    conn.execute(
        f"INSERT INTO runs (run_id, started_at) VALUES ({conn.dialect.placeholder}, {conn.dialect.placeholder})",
        (run_id, started_at),
    )


def _work_item_short_id(conn, initiative, task_id):
    row = conn.query_one(
        f"SELECT short_id FROM work_items WHERE initiative = {conn.dialect.placeholder} "
        f"AND task_id = {conn.dialect.placeholder}",
        (initiative, task_id),
    )
    return None if row is None else row[0]


def _run_short_id(conn, run_id):
    row = conn.query_one(f"SELECT short_id FROM runs WHERE run_id = {conn.dialect.placeholder}", (run_id,))
    return None if row is None else row[0]


def test_allocate_initiative_returns_i1_then_i2(store_conn):
    assert allocate_initiative(store_conn) == "I1"
    assert allocate_initiative(store_conn) == "I2"


def _seed_three_initiatives(conn):
    # alpha: earliest overall, via a run that starts before its own intake row.
    _item(conn, "alpha", "intake", "intake", "2026-01-05T00:00:00Z")
    _item(conn, "alpha", "t1-foo", "p1", "2026-01-05T00:00:00Z")
    _run(conn, "alpha-3", "2026-01-01T00:00:00Z")
    # x: second by its intake-row date, which is earlier than its run.
    _item(conn, "x", "intake", "intake", "2026-01-02T00:00:00Z")
    _item(conn, "x", "t1-bar", "p1", "2026-01-02T00:00:00Z")
    _run(conn, "x-7", "2026-01-03T00:00:00Z")
    # zeta: no runs, latest intake-row date.
    _item(conn, "zeta", "intake", "intake", "2026-01-04T00:00:00Z")
    _item(conn, "zeta", "t1-baz", "p1", "2026-01-04T00:00:00Z")


def test_backfill_numbers_by_first_appearance_and_a_run_gets_its_initiatives_number(store_conn):
    _seed_three_initiatives(store_conn)

    counts = backfill(store_conn)

    assert counts == {"initiatives": 3, "work_items": 6, "runs": 2}
    assert _work_item_short_id(store_conn, "alpha", "intake") == "I1"
    assert _work_item_short_id(store_conn, "alpha", "t1-foo") == "I1-t1"
    assert _run_short_id(store_conn, "alpha-3") == "I1-3"
    assert _work_item_short_id(store_conn, "x", "intake") == "I2"
    assert _work_item_short_id(store_conn, "x", "t1-bar") == "I2-t1"
    assert _run_short_id(store_conn, "x-7") == "I2-7"
    assert _work_item_short_id(store_conn, "zeta", "intake") == "I3"
    assert _work_item_short_id(store_conn, "zeta", "t1-baz") == "I3-t1"
    assert allocate_initiative(store_conn) == "I4"


def test_backfill_is_a_no_op_on_a_second_call(store_conn):
    _seed_three_initiatives(store_conn)
    backfill(store_conn)

    counts = backfill(store_conn)

    assert counts == {"initiatives": 0, "work_items": 0, "runs": 0}
    assert _work_item_short_id(store_conn, "alpha", "intake") == "I1"
    assert _run_short_id(store_conn, "x-7") == "I2-7"


def test_backfill_tops_up_rows_added_to_an_already_numbered_initiative(store_conn):
    _seed_three_initiatives(store_conn)
    backfill(store_conn)
    _item(store_conn, "alpha", "t2-new", "p2", "2026-01-06T00:00:00Z")
    _run(store_conn, "alpha-9", "2026-01-06T00:00:00Z")

    counts = backfill(store_conn)

    assert counts == {"initiatives": 0, "work_items": 1, "runs": 1}
    assert _work_item_short_id(store_conn, "alpha", "t2-new") == "I1-t2"
    assert _run_short_id(store_conn, "alpha-9") == "I1-9"
    assert _work_item_short_id(store_conn, "alpha", "t1-foo") == "I1-t1"
    assert allocate_initiative(store_conn) == "I4"


def test_resolve_finds_the_same_run_by_short_id_or_old_key(store_conn):
    _seed_three_initiatives(store_conn)
    backfill(store_conn)

    assert resolve(store_conn, "I2-7") == ("run", "x-7")
    assert resolve(store_conn, "x-7") == ("run", "x-7")


def test_resolve_finds_the_same_work_item_by_short_id_or_old_key(store_conn):
    _seed_three_initiatives(store_conn)
    backfill(store_conn)

    assert resolve(store_conn, "I2-t1") == ("work_item", "x/t1-bar")
    assert resolve(store_conn, "x/t1-bar") == ("work_item", "x/t1-bar")


def test_resolve_returns_none_for_an_unknown_token(store_conn):
    assert resolve(store_conn, "I99") is None
    assert resolve(store_conn, "no-such-run") is None
