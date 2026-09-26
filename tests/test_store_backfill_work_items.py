import os
from datetime import UTC, datetime

import pytest

from harness.store_backfill import backfill, parse_work_item, work_item_paths
from harness.store_read import work_items
from harness.store_write import Store, upsert_work_item

MTIME = 1_780_000_000.0
FILE_TIME = datetime.fromtimestamp(MTIME, UTC).isoformat()
NEWER = "2099-01-01T00:00:00+00:00"


def task(id_, phase, state, needs="[]"):
    return f"---\nid: {id_}\nphase: {phase}\nstate: {state}\nneeds: {needs}\n---\nbody\n"


FILES = {
    "alpha/p1/a1.md": task("a1", "p1", "ready"),
    "alpha/p2/a2.md": task("a2", "p2", "blocked", "[a1]"),
    "beta/p1/b1.md": task("b1", "p1", "done"),
    "alpha/initiative.md": "---\nid: alpha\ntitle: not a task\n---\n",
    "beta/p1/bad.md": "---\nid: [unclosed\n---\n",
}


@pytest.fixture
def work(tmp_path):
    root = tmp_path / "work"
    for name, text in FILES.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        os.utime(path, (MTIME, MTIME))
    (tmp_path / "runs").mkdir()
    (tmp_path / "ledger.jsonl").write_text("", encoding="utf-8")
    return tmp_path / "runs", root, tmp_path / "ledger.jsonl"


def stored(conn):
    return [
        (i, r["task_id"], r["phase"], r["state"], r["needs"], r["updated_at"], r["updated_by"])
        for i in ("alpha", "beta")
        for r in work_items(conn, i)
    ]


def test_three_task_files_give_three_rows_and_a_rerun_leaves_the_same_rows(store_conn, work):
    store = Store(store_conn)
    with pytest.warns(UserWarning, match="beta/p1/bad.md"):
        report = backfill(store, *work)
    first = stored(store_conn)
    assert first == [
        ("alpha", "a1", "p1", "ready", [], FILE_TIME, "backfill"),
        ("alpha", "a2", "p2", "blocked", ["a1"], FILE_TIME, "backfill"),
        ("beta", "b1", "p1", "done", [], FILE_TIME, "backfill"),
    ]
    assert report["work_items_upserted"] == 3
    assert report["work_item_disagreements"] == []
    assert report["malformed_work_items"] == ["beta/p1/bad.md: no readable id and state"]
    with pytest.warns(UserWarning):
        backfill(store, *work)
    assert stored(store_conn) == first


def test_a_newer_stored_row_with_another_state_stays_and_is_reported(store_conn, work):
    upsert_work_item(store_conn, "alpha", "a1", "p1", "done", [], NEWER, "human")
    with pytest.warns(UserWarning):
        report = backfill(Store(store_conn), *work)
    assert stored(store_conn)[0] == ("alpha", "a1", "p1", "done", [], NEWER, "human")
    assert len(stored(store_conn)) == 3
    assert report["work_items_upserted"] == 2
    assert [(d["task_id"], d["file_state"], d["store_state"]) for d in report["work_item_disagreements"]] == [
        ("a1", "ready", "done")
    ]


def test_initiative_md_is_not_a_task_and_the_initiative_is_the_directory(work):
    found = [(p.name, initiative, phase) for p, initiative, phase in work_item_paths(work[1])]
    assert found == [
        ("a1.md", "alpha", "p1"),
        ("a2.md", "alpha", "p2"),
        ("b1.md", "beta", "p1"),
        ("bad.md", "beta", "p1"),
    ]


def test_a_missing_id_or_state_is_no_item_and_a_missing_phase_is_its_directory():
    assert parse_work_item({"state": "ready", "phase": "p1"}, "p1") is None
    assert parse_work_item({"id": "a", "phase": "p1"}, "p1") is None
    assert parse_work_item({"id": "a", "state": "ready", "needs": "b"}, "p1") is None
    assert parse_work_item({"id": "a", "state": "ready"}, "p9") == {
        "id": "a",
        "phase": "p9",
        "state": "ready",
        "needs": [],
    }
