import json
from pathlib import Path

import pytest

from harness import store_backfill_traces as bf

INIT = {"type": "system", "subtype": "init", "model": "claude-haiku-4-5-20251001", "claude_code_version": "2.1.280"}


def _events(n: int) -> list[dict]:
    return [INIT] + [{"type": "assistant", "n": i} for i in range(1, n)]


def _write_trace(loose: Path, run_id: str, name: str, events: list[dict]) -> Path:
    path = loose / f"{run_id}-trace" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    return path


def _write_calls(calls: Path, run_id: str, rows: list[dict]) -> None:
    calls.mkdir(parents=True, exist_ok=True)
    (calls / f"{run_id}.calls.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


@pytest.fixture
def tree(tmp_path):
    loose, calls = tmp_path / "loose", tmp_path / "calls"
    _write_trace(loose, "r1", "build-1.jsonl", _events(3))
    _write_trace(loose, "r1", "review-1.jsonl", _events(2))
    _write_trace(loose, "r2", "build-1.jsonl", _events(4))
    _write_calls(
        calls,
        "r1",
        [
            {"id": "c1", "role": "build", "ts": "2026-09-23T23:59:00Z", "trace": "traces/r1-trace/build-1.jsonl"},
            {"id": "c2", "role": "review", "ts": "2026-09-24T00:01:00Z", "trace": "traces/r1-trace/review-1.jsonl"},
        ],
    )
    _write_calls(
        calls, "r2", [{"id": "c3", "role": "build", "ts": "2026-09-24T10:00:00Z", "trace": "r2-trace/build-1.jsonl"}]
    )
    return loose, calls


def test_parse_trace_name_reads_role_and_one_based_index():
    assert bf.parse_trace_name("build-2.jsonl") == ("build", 2)
    assert bf.parse_trace_name("review-pass-10.jsonl") == ("review-pass", 10)
    assert bf.parse_trace_name("notes.txt") is None


def test_plan_moves_matches_on_run_and_final_file_name():
    calls = [bf.Call("r1", "c1", "build", "2026-09-23T01:00:00Z", "x/y/build-1.jsonl")]
    a = bf.TraceFile("r1", "build", 1, Path("/l/r1-trace/build-1.jsonl"))
    b = bf.TraceFile("r2", "build", 1, Path("/l/r2-trace/build-1.jsonl"))
    plan = bf.plan_moves(calls, [a, b], lambda p: "2026-01-02")
    assert plan[a] == bf.Move("2026-09-23", "r1", "c1", True)
    assert plan[b] == bf.Move("2026-01-02", "r2", "r2-build-1", False)


def test_loaders_read_the_fixture_tree(tree):
    loose, calls = tree
    assert [(t.run_id, t.role, t.index) for t in bf.find_trace_files(loose)] == [
        ("r1", "build", 1),
        ("r1", "review", 1),
        ("r2", "build", 1),
    ]
    assert [c.id for c in bf.load_calls(calls)] == ["c1", "c2", "c3"]
    assert bf.read_events(loose / "r1-trace" / "build-1.jsonl") == _events(3)
