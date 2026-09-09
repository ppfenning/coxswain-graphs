"""record_usage reads the per-call ledger file and unions it with runner.calls; a bad line is skipped."""

from __future__ import annotations

import json
from types import SimpleNamespace

from harness.usage import record_usage

_ROW_A = {
    "role": "build",
    "model": "claude-x",
    "cost_usd": 1.0,
    "turns": 3,
    "input_tokens": 100,
    "output_tokens": 50,
    "trace": "runs/run1-trace/build-1.jsonl",
    "ts": "2026-09-08T00:00:00+00:00",
    "ok": True,
}
_ROW_B = {
    "role": "plan",
    "model": "claude-y",
    "cost_usd": 2.0,
    "turns": 1,
    "input_tokens": 10,
    "output_tokens": 20,
    "ts": "2026-09-08T00:01:00+00:00",
    "ok": False,
    "error": "boom",
}


def test_two_ledger_lines_aggregate_into_usage_json(tmp_path) -> None:
    (tmp_path / "run1.calls.jsonl").write_text(json.dumps(_ROW_A) + "\n" + json.dumps(_ROW_B) + "\n", encoding="utf-8")

    summary = record_usage(SimpleNamespace(calls=[]), runs_dir=tmp_path, run_id="run1")

    assert summary["calls"] == 2
    assert summary["cost_usd"] == 3.0
    written = json.loads((tmp_path / "run1.usage.json").read_text(encoding="utf-8"))
    assert written["summary"]["calls"] == 2
    assert len(written["calls"]) == 2


def test_a_malformed_line_between_two_good_ones_is_skipped_not_fatal(tmp_path) -> None:
    lines = "\n".join([json.dumps(_ROW_A), "{not json", json.dumps(_ROW_B)]) + "\n"
    (tmp_path / "run2.calls.jsonl").write_text(lines, encoding="utf-8")

    summary = record_usage(SimpleNamespace(calls=[]), runs_dir=tmp_path, run_id="run2")

    assert summary["calls"] == 2


def test_runner_calls_fill_in_what_the_ledger_is_missing_but_the_ledger_wins_a_duplicate(tmp_path) -> None:
    (tmp_path / "run3.calls.jsonl").write_text(json.dumps(_ROW_A) + "\n" + json.dumps(_ROW_B) + "\n", encoding="utf-8")
    duplicate_of_a = {**_ROW_A, "cost_usd": 999.0}
    no_trace = {"role": "review", "model": "claude-z", "cost_usd": 5.0, "turns": 1, "ok": True}
    third = {"role": "arbitration", "model": "claude-z", "cost_usd": 7.0, "turns": 1, "trace": "t3", "ok": True}
    runner = SimpleNamespace(calls=[duplicate_of_a, no_trace, third])

    summary = record_usage(runner, runs_dir=tmp_path, run_id="run3")

    assert summary["calls"] == 4
    written = json.loads((tmp_path / "run3.usage.json").read_text(encoding="utf-8"))
    build_rows = [c for c in written["calls"] if c["role"] == "build"]
    assert len(build_rows) == 1
    assert build_rows[0]["cost_usd"] == 1.0
