import json

import pytest

import harness.store_copy as sc
from harness.store_migrate import open_store

T0 = "2026-09-25T00:00:00Z"

NESTED = '{"z": 1, "a": {"b": [1, 2]}, "name": "café"}'
ROWS = [
    ("r1", "p1", "t1", NESTED, "2026-09-25T01:00:00Z"),
    ("r1", "p1", "t2", '{"state": "done"}', "2026-09-25T02:00:00Z"),
    ("r1", "p2", "t1", "{}", "2026-09-25T03:00:00Z"),
]
SELECT = (
    "SELECT run_id, phase_id, task_id, record_json, updated_at FROM task_records ORDER BY run_id, phase_id, task_id"
)
INSERT = "INSERT INTO task_records (run_id, phase_id, task_id, record_json, updated_at) VALUES ({})"


@pytest.fixture
def src_url(tmp_path):
    url = f"sqlite:///{tmp_path / 'src.db'}"
    conn = open_store(url, T0)
    try:
        sql = INSERT.format(", ".join(conn.dialect.placeholder for _ in range(5)))
        for row in ROWS:
            conn.execute(sql, row)
    finally:
        conn.close()
    return url


@pytest.fixture
def dst_url(store_url, tmp_path):
    """The destination per backend. An in-memory sqlite store vanishes on close, so it becomes a file."""
    return f"sqlite:///{tmp_path / 'dst.db'}" if store_url == "sqlite:///:memory:" else store_url


def rows(url):
    """Rows with record_json parsed: Postgres JSONB hands back a value, sqlite the text."""
    conn = open_store(url, T0)
    try:
        return [(*r[:3], json.loads(r[3]) if isinstance(r[3], str) else r[3], r[4]) for r in conn.query_all(SELECT)]
    finally:
        conn.close()


def test_task_records_arrive_unchanged(src_url, dst_url):
    report = sc.copy(src_url, dst_url, T0)
    assert report["task_records"] == {"source": 3, "copied": 3, "present": 0}
    assert rows(dst_url) == rows(src_url)
    assert rows(dst_url)[0][3] == json.loads(NESTED)


def test_a_second_copy_does_not_duplicate_task_records(src_url, dst_url):
    sc.copy(src_url, dst_url, T0)
    again = sc.copy(src_url, dst_url, T0)
    assert again["task_records"] == {"source": 3, "copied": 0, "present": 3}
    assert len(rows(dst_url)) == 3
