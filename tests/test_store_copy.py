import json

import pytest

import harness.store_copy as sc
from harness.store_migrate import open_store

T0 = "2026-09-25T00:00:00Z"

EXPECTED_ORDER = (
    "graphs",
    "graph_nodes",
    "graph_edges",
    "runs",
    "phases",
    "tasks",
    "task_records",
    "attempts",
    "node_calls",
    "gate_decisions",
    "ledger",
    "leases",
)

SEED = {
    "graphs": [{"graph_id": "g1", "name": "demo", "version": "1", "content_hash": "g1", "definition_json": '{"x": 1}'}],
    "graph_nodes": [{"graph_id": "g1", "node_id": "n1", "ord": 0, "role": "plan"}],
    "graph_edges": [{"graph_id": "g1", "src": "n1", "dst": "n2"}],
    "runs": [{"run_id": "r1", "principal": "pat", "status": "done", "graph_id": "g1", "record_json": '{"a": 1}'}],
    "phases": [{"run_id": "r1", "phase_id": "p1", "human_minutes": 1.5, "totals_json": "{}"}],
    "tasks": [{"run_id": "r1", "task_id": "t1", "phase_id": "p1", "state": "done"}],
    "task_records": [
        {"run_id": "r1", "phase_id": "p1", "task_id": "t1", "record_json": '{"s": "done"}', "updated_at": T0}
    ],
    "attempts": [{"run_id": "r1", "task_id": "t1", "seq": 1, "kind": "build"}],
    "node_calls": [
        {"call_id": "c1", "run_id": "r1", "seq": 1, "cost_usd": 0.25, "ok": 1, "node_id": "n1"},
        {"call_id": "c2", "run_id": "r1", "seq": 2, "cost_usd": 0.5, "ok": 0, "detail_json": '{"k": "v"}'},
    ],
    "gate_decisions": [{"run_id": "r1", "phase_id": "p1", "seq": 1, "kind": "merge", "applied": 1, "epoch": 3}],
    "ledger": [{"row_hash": "h1", "run_id": "r1", "kind": "merge", "epoch": 3, "row_json": "{}"}],
    "leases": [{"name": "epic", "holder": "me", "epoch": 4}],
}


def put(conn, table, row):
    marks = ", ".join(conn.dialect.placeholder for _ in row)
    conn.execute(f"INSERT INTO {table} ({', '.join(row)}) VALUES ({marks})", list(row.values()))


def seed(conn, extra=None):
    for name, rows in {**SEED, **(extra or {})}.items():
        for row in rows:
            put(conn, name, row)


def counts(url):
    conn = open_store(url, T0)
    try:
        return {n: conn.query_one(f"SELECT COUNT(*) FROM {n}")[0] for n, _, _ in sc.tables()}
    finally:
        conn.close()


@pytest.fixture
def src_url(tmp_path):
    url = f"sqlite:///{tmp_path / 'src.db'}"
    conn = open_store(url, T0)
    try:
        seed(conn)
    finally:
        conn.close()
    return url


@pytest.fixture
def dst_url(store_url, tmp_path):
    """The destination per backend. An in-memory sqlite store vanishes on close, so it becomes a file."""
    return f"sqlite:///{tmp_path / 'dst.db'}" if store_url == "sqlite:///:memory:" else store_url


def test_the_plan_puts_parents_first_and_names_every_table():
    assert tuple(n for n, _, _ in sc.plan(sc.tables())) == EXPECTED_ORDER


def test_a_table_the_order_does_not_know_is_refused():
    with pytest.raises(ValueError, match="mystery"):
        sc.plan((*sc.tables(), ("mystery", ("a",), ("a",))))


def test_the_columns_come_from_the_ddl_including_the_alter_columns():
    by_name = {n: cols for n, cols, _ in sc.tables()}
    assert "graph_id" in by_name["runs"]
    assert "node_id" in by_name["node_calls"]
    assert "schema_version" not in by_name


def test_the_runs_host_column_is_copied_including_null(src_url, dst_url):
    src = open_store(src_url, T0)
    try:
        put(src, "runs", {"run_id": "rh1", "host": "build-host-1"})
        put(src, "runs", {"run_id": "rh2", "host": None})
    finally:
        src.close()
    sc.copy(src_url, dst_url, T0)
    dst = open_store(dst_url, T0)
    try:
        assert dst.query_all("SELECT run_id, host FROM runs WHERE run_id IN ('rh1', 'rh2') ORDER BY run_id") == [
            ("rh1", "build-host-1"),
            ("rh2", None),
        ]
    finally:
        dst.close()


def test_every_source_row_lands_in_the_destination(src_url, dst_url):
    report = sc.copy(src_url, dst_url, T0)
    assert report["node_calls"] == {"source": 2, "copied": 2, "present": 0}
    assert {n: r["copied"] for n, r in report.items()} == {n: len(SEED[n]) for n in EXPECTED_ORDER}
    assert counts(dst_url) == {n: len(SEED[n]) for n in EXPECTED_ORDER}
    dst = open_store(dst_url, T0)
    try:
        assert dst.query_one("SELECT graph_id FROM runs") == ("g1",)
        assert dst.query_all("SELECT call_id, cost_usd, ok, node_id FROM node_calls ORDER BY call_id") == [
            ("c1", 0.25, 1, "n1"),
            ("c2", 0.5, 0, None),
        ]
        assert json.loads(dst.query_one("SELECT definition_json FROM graphs")[0]) == {"x": 1}
    finally:
        dst.close()


def test_a_second_copy_inserts_nothing_and_reports_every_row_present(src_url, dst_url):
    sc.copy(src_url, dst_url, T0)
    again = sc.copy(src_url, dst_url, T0)
    assert {n: (r["copied"], r["present"]) for n, r in again.items()} == {n: (0, len(SEED[n])) for n in EXPECTED_ORDER}
    assert counts(dst_url) == {n: len(SEED[n]) for n in EXPECTED_ORDER}


def test_a_batch_smaller_than_the_table_still_copies_every_row(src_url, dst_url):
    assert sc.copy(src_url, dst_url, T0, batch=1)["node_calls"]["copied"] == 2


def test_a_destination_with_extra_rows_still_passes_the_check(src_url, dst_url):
    dst = open_store(dst_url, T0)
    try:
        put(dst, "runs", {"run_id": "extra"})
        put(dst, "leases", {"name": "other"})
    finally:
        dst.close()
    report = sc.copy(src_url, dst_url, T0)
    assert report["runs"] == {"source": 1, "copied": 1, "present": 0}
    assert (counts(dst_url)["runs"], counts(dst_url)["leases"]) == (2, 2)


def test_a_shortfall_raises_with_the_report_and_main_exits_one(src_url, dst_url, monkeypatch, capsys):
    monkeypatch.setattr(sc, "_count", lambda conn, table: 0)
    with pytest.raises(sc.CopyCheckFailed) as failed:
        sc.copy(src_url, dst_url, T0)
    assert failed.value.report["runs"]["source"] == 1
    assert sc.main([src_url, dst_url]) == 1
    assert "FAIL runs: destination has 0, source has 1" in capsys.readouterr().out


def test_main_prints_a_line_per_table_and_exits_zero(src_url, dst_url, capsys):
    assert sc.main([src_url, dst_url]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert "node_calls source=2 copied=2 present=0" in lines
    assert len(lines) == 1 + len(EXPECTED_ORDER)


def test_main_json_is_one_object(src_url, dst_url, capsys):
    assert sc.main([src_url, dst_url, "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert (out["ok"], out["tables"]["leases"]) == (True, {"source": 1, "copied": 1, "present": 0})


def test_safe_url_keeps_only_scheme_and_path():
    assert sc._safe_url("postgresql://user:hunter2@db.example:5432/store?sslmode=require") == "postgresql:///store"
    assert sc._safe_url("sqlite:////tmp/x.db") == "sqlite:////tmp/x.db"


def test_a_password_never_reaches_output_even_when_the_copy_fails(src_url, capsys):
    assert sc.main([src_url, "postgresql://user:hunter2@127.0.0.1:1/store"]) == 2
    seen = capsys.readouterr()
    assert "hunter2" not in seen.out + seen.err
