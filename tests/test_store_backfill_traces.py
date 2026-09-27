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


def test_load_calls_skips_a_row_written_before_calls_had_an_id(tmp_path):
    (tmp_path / "r0.calls.jsonl").write_text(
        '{"role": "build", "ts": "2026-09-10T00:00:00+00:00"}\n{"id": "c9", "role": "build", "ts": "2026-09-10T00:01:00+00:00"}\n'
    )
    assert [c.id for c in bf.load_calls(tmp_path)] == ["c9"]


def test_loaders_read_the_fixture_tree(tree):
    loose, calls = tree
    assert [(t.run_id, t.role, t.index) for t in bf.find_trace_files(loose)] == [
        ("r1", "build", 1),
        ("r1", "review", 1),
        ("r2", "build", 1),
    ]
    assert [c.id for c in bf.load_calls(calls)] == ["c1", "c2", "c3"]
    assert bf.read_events(loose / "r1-trace" / "build-1.jsonl") == _events(3)


def _archived(run_id="r1", day="2026/09/24", src_bytes=100) -> bf.ArchivedRun:
    return bf.ArchivedRun(run_id, day, src_bytes, {"a": 3, "b": 2, "c": 4})


def test_unequal_calls_names_the_calls_that_differ_and_counts_a_missing_call_as_zero():
    assert bf.unequal_calls({"a": 3, "b": 2}, {"a": 3, "b": 2, "x": 9}) == ()
    assert bf.unequal_calls({"a": 3, "b": 2, "c": 1}, {"a": 4, "b": 2}) == ("a", "c")


def test_verified_counts_give_prune():
    assert bf.prune_verdict(_archived(), {"a": 3, "b": 2, "c": 4}) == bf.PRUNE


def test_more_parquet_events_than_the_sources_still_give_prune():
    assert bf.prune_verdict(_archived(), {"a": 3, "b": 5, "c": 4}) == bf.PRUNE


def test_a_missing_parquet_gives_keep_with_no_parquet():
    assert bf.prune_verdict(_archived(), None) == bf.Verdict(False, "no parquet")


def test_an_unreadable_parquet_gives_keep_with_the_error():
    assert bf.prune_verdict(_archived(), "bad footer") == bf.Verdict(False, "unreadable: bad footer")


def test_one_call_short_of_three_keeps_the_whole_run_and_names_the_call():
    verdict = bf.prune_verdict(_archived(), {"a": 3, "b": 1, "c": 4})
    assert not verdict.prune
    assert "call b" in verdict.reason


def test_a_call_absent_from_the_parquet_keeps_the_run_and_names_the_call():
    verdict = bf.prune_verdict(_archived(), {"a": 3, "b": 2})
    assert not verdict.prune
    assert "call c" in verdict.reason


def _rows():
    return [
        (_archived("r2", "2026/09/25", 50), bf.PRUNE),
        (_archived("r3", "2026/09/24", 7), bf.Verdict(False, "no parquet")),
        (_archived("r1", "2026/09/24", 100), bf.PRUNE),
    ]


def test_report_lines_sort_by_day_then_run_id_and_total_only_pruned_bytes():
    assert bf.report_lines(_rows(), dry_run=True) == [
        "r1 2026/09/24 100 prune",
        "r3 2026/09/24 7 keep: no parquet",
        "r2 2026/09/25 50 prune",
        "total would free: 150 bytes",
    ]


def test_dry_and_real_report_lines_differ_only_in_the_total_label():
    dry, real = bf.report_lines(_rows(), dry_run=True), bf.report_lines(_rows(), dry_run=False)
    assert dry[:-1] == real[:-1]
    assert real[-1] == "total freed: 150 bytes"
