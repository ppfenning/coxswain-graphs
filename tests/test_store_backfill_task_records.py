import json
import os
from datetime import UTC, datetime

import pytest

from harness.resume import result_path
from harness.store_backfill import backfill, balanced
from harness.store_dialect import json_load
from harness.store_write import Store

STAMP = "2026-01-01T00:00:00+00:00"
MTIME = 1_780_000_000.0
FIRST = {"id": "t1", "status": "approved", "proposals": [{"kind": "draft_pr_create"}]}
SECOND = {"id": "t2", "status": "quarantined", "quarantine": "patch does not apply"}


@pytest.fixture
def paths(tmp_path):
    runs = tmp_path / "runs"
    for phase, task, record in (("p1", "t1", FIRST), ("p2", "t2", SECOND)):
        path = result_path(runs, "run-a", phase, task)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(record), encoding="utf-8")
        os.utime(path, (MTIME, MTIME))
    bad = result_path(runs, "run-a", "p1", "t3")
    bad.write_text("{not json", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("", encoding="utf-8")
    return runs, work, ledger


def rows(conn):
    sql = "SELECT run_id, phase_id, task_id, record_json, updated_at FROM task_records ORDER BY phase_id, task_id"
    return [(r[0], r[1], r[2], json_load(r[3]), r[4]) for r in conn.query_all(sql)]


def test_two_files_give_two_rows_and_a_rerun_leaves_the_same_rows(store_conn, paths):
    store = Store(store_conn)
    with pytest.warns(UserWarning, match="p1/t3.json"):
        report = backfill(store, *paths)
    first = rows(store_conn)
    assert [r[:4] for r in first] == [("run-a", "p1", "t1", FIRST), ("run-a", "p2", "t2", SECOND)]
    assert report["task_records_imported"] == 2
    assert report["malformed_task_records"] == ["run-a/tasks/p1/t3.json: not a JSON object"]
    assert balanced(report)
    with pytest.warns(UserWarning):
        backfill(store, *paths)
    assert rows(store_conn) == first


def test_the_caller_timestamp_is_the_updated_at(store_conn, paths):
    with pytest.warns(UserWarning):
        backfill(Store(store_conn), *paths, task_records_updated_at=STAMP)
    assert {r[4] for r in rows(store_conn)} == {STAMP}


def test_without_a_timestamp_the_file_mtime_is_the_updated_at(store_conn, paths):
    with pytest.warns(UserWarning):
        backfill(Store(store_conn), *paths)
    assert {r[4] for r in rows(store_conn)} == {datetime.fromtimestamp(MTIME, UTC).isoformat()}
