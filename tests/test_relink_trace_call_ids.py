from pathlib import Path

import pytest

pytest.importorskip("pyarrow")

from harness import store_backfill_traces as bf
from harness import store_traces
from harness.store_dialect import json_text
from harness.store_migrate import open_store
from harness.traces_url import resolve_traces_root

NOW = "2026-09-24T00:00:00Z"
DAY = "2026-09-20"
EVENTS = {
    "r1-build-1": [{"type": "system", "subtype": "init"}, {"type": "assistant", "n": 1}],
    "r1-plan-1": [{"type": "system", "subtype": "init"}],
    "r1-real": [{"type": "assistant", "n": 2}],
}
LINKS = {"r1-build-1": "legacy:r1:0", "r1-plan-1": "legacy:r1:1"}


def _put(conn, table, **row):
    marks = ", ".join(conn.dialect.placeholder for _ in row)
    conn.execute(f"INSERT INTO {table} ({', '.join(row)}) VALUES ({marks})", tuple(row.values()))


def _call(conn, call_id, seq, detail):
    _put(conn, "node_calls", call_id=call_id, run_id="r1", seq=seq, role="build", ts=NOW, detail_json=detail)


@pytest.fixture
def world(tmp_path):
    url = f"sqlite:///{tmp_path / 'cox.db'}"
    conn = open_store(url, NOW)
    _put(conn, "runs", run_id="r1", status="done", started_at=NOW)
    _call(conn, "legacy:r1:0", 0, json_text({"trace": "/runs/r1-trace/build-1.jsonl"}))
    _call(conn, "legacy:r1:1", 1, json_text({"trace": "plan-1.jsonl"}))
    _call(conn, "real-id", 2, json_text({"trace": "/runs/r1-trace/real.jsonl"}))
    conn.close()
    root = tmp_path / "traces"
    traces = resolve_traces_root(str(root), Path("."), {})
    store_traces.write_run(traces, DAY, "r1", EVENTS)
    store_traces.write_run(traces, DAY, "r2", {"r2-build-1": [{"type": "assistant"}]})
    return url, traces, root


def _rows(traces, run_id):
    return sorted(store_traces.iter_run(traces, run_id), key=lambda r: (r["call_id"], r["seq"]))


def _events(traces, run_id):
    return sorted((r["seq"], r["event"]) for r in store_traces.iter_run(traces, run_id))


def _relink(root, url, *flags, capsys):
    code = bf.main(["relink", str(root), "--store-url", url, *flags])
    return code, capsys.readouterr().out


def test_relink_map_keys_the_synthetic_id_to_the_store_id_for_legacy_calls_with_a_trace():
    calls = [
        bf.Call("r1", "legacy:r1:0", "", "", "/x/r1-trace/build-1.jsonl"),
        bf.Call("r1", "legacy:r1:1", "", "", "plan-1.jsonl"),
        bf.Call("r1", "real-id", "", "", "real.jsonl"),
    ]
    assert bf.relink_map(calls) == LINKS


def test_relink_rewrites_call_ids_and_leaves_events_alone(world, capsys):
    url, traces, root = world
    before, other = _events(traces, "r1"), _rows(traces, "r2")
    code, out = _relink(root, url, capsys=capsys)
    assert code == 0
    assert sorted({r["call_id"] for r in _rows(traces, "r1")}) == ["legacy:r1:0", "legacy:r1:1", "r1-real"]
    assert _events(traces, "r1") == before
    assert _rows(traces, "r2") == other
    assert out.splitlines() == ["relinked run r1: 3 rows", "relinked: 1 runs, 3 rows"]


def test_a_second_relink_changes_nothing_and_says_so(world, capsys):
    url, traces, root = world
    _relink(root, url, capsys=capsys)
    path = Path(store_traces.run_parquet(traces.path, DAY, "r1"))
    first = path.read_bytes()
    code, out = _relink(root, url, capsys=capsys)
    assert (code, out.strip()) == (0, "nothing to change")
    assert path.read_bytes() == first


def test_dry_run_reports_and_writes_nothing(world, capsys):
    url, traces, root = world
    path = Path(store_traces.run_parquet(traces.path, DAY, "r1"))
    before = path.read_bytes()
    code, out = _relink(root, url, "--dry-run", capsys=capsys)
    assert code == 0
    assert out.splitlines() == ["would relink run r1: 3 rows", "would relink: 1 runs, 3 rows"]
    assert path.read_bytes() == before
    assert not path.with_name(f"{path.name}.tmp").exists()


def test_relink_rows_builds_new_rows_and_counts_the_changed_ones():
    rows = [{"call_id": "a", "seq": 0}, {"call_id": "b", "seq": 0}]
    new, n = bf.relink_rows(rows, {"a": "legacy:x"})
    assert (new, n, rows[0]["call_id"]) == ([{"call_id": "legacy:x", "seq": 0}, {"call_id": "b", "seq": 0}], 1, "a")
