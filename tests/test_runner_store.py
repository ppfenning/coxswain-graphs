"""Both runners record each finished call in the run-record store, and a store fault never changes the call."""

from __future__ import annotations

import json
import logging
import stat
import warnings
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from harness.store_migrate import open_store
from harness.store_write import Store
from runner.anthropic_runner import AnthropicRunner
from runner.claude_code_runner import ClaudeCodeRunner
from runner.protocol import RunnerError

PROFILE = {"tiers": {"cheap": "haiku", "standard": "sonnet", "deep": "opus"}}
SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
RUN = "run-1:p3-write"
# The shape of the documented Claude Code system/init message, as tests/test_claude_code_runner.py uses it.
INIT = {"type": "system", "subtype": "init", "claude_code_version": "2.0.31", "model": "claude-haiku-4-5-20251001"}
RESULT = {"type": "result", "subtype": "success", "is_error": False, "structured_output": {"ok": True}, "num_turns": 1}
NOT_JSON = {**RESULT, "structured_output": None, "result": "prose, not json"}


class _Boom:
    def record_call(self, *args: Any, **kwargs: Any) -> int:
        raise RuntimeError("disk full")


@pytest.fixture
def conn():
    c = open_store("sqlite:///:memory:", "2026-09-24T00:00:00Z")
    yield c
    c.close()


def _rows(conn) -> list[tuple[Any, ...]]:
    return conn.query_all(
        "SELECT run_id, phase_id, role, ok, claude_code_version, model_id, chosen_tier FROM node_calls ORDER BY seq"
    )


def _claude(tmp_path: Path, events: list[dict], **kwargs: Any) -> ClaudeCodeRunner:
    (tmp_path / "output.json").write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    script = tmp_path / "claude"
    script.write_text(f"#!/bin/sh\ncat {tmp_path / 'output.json'}\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return ClaudeCodeRunner(
        PROFILE, claude_bin=str(script), cwd=tmp_path, trace_dir=tmp_path / "trace", runs_dir=tmp_path / "runs", **kwargs
    )


def _go(runner: ClaudeCodeRunner) -> Any:
    return runner.run(role="build", tier="standard", schema=SCHEMA, prompt="go", task="t1")


def test_a_call_leaves_one_row_carrying_the_init_events_version_and_model(tmp_path, conn) -> None:
    _go(_claude(tmp_path, [INIT, RESULT], store=Store(conn), run_id=RUN))
    assert _rows(conn) == [("run-1", "p3-write", "build", 1, "2.0.31", "claude-haiku-4-5-20251001", "reason")]


def test_a_call_records_the_stream_summary_in_the_call_dict_and_in_detail_json(tmp_path, conn) -> None:
    read = {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "r1", "name": "Read", "input": {"file_path": "/a/b.py"}}]}}
    runner = _claude(tmp_path, [INIT, read, RESULT], store=Store(conn), run_id=RUN)
    _go(runner)
    expected = {"tool_uses": {"Read": 1}, "reads": {"b.py": 1}, "whole_file_reads": 1, "result": "success", "is_error": False}
    assert runner.calls[-1]["summary"] == expected
    (detail,) = conn.query_all("SELECT detail_json FROM node_calls")[0]
    assert (json.loads(detail) if isinstance(detail, str) else detail)["summary"] == expected


def test_with_no_store_no_row_is_written_and_a_call_writes_no_calls_file(tmp_path, conn) -> None:
    (tmp_path / "bare").mkdir()
    (tmp_path / "stored").mkdir()
    _go(_claude(tmp_path / "bare", [INIT, RESULT], run_id=RUN))
    assert _rows(conn) == []
    _go(_claude(tmp_path / "stored", [INIT, RESULT], run_id=RUN, store=Store(conn)))
    assert len(_rows(conn)) == 1
    assert not list(tmp_path.rglob("*.calls.jsonl"))
    assert list((tmp_path / "stored" / "trace").glob("build-*.jsonl")), "the per-call trace file is still written"


def test_a_runner_error_call_still_leaves_a_row_with_ok_zero(tmp_path, conn) -> None:
    runner = _claude(tmp_path, [INIT, NOT_JSON], store=Store(conn), run_id=RUN)
    with pytest.raises(RunnerError):
        _go(runner)
    assert [(r[2], r[3]) for r in _rows(conn)] == [("build", 0)]


def test_a_raising_store_leaves_the_return_value_and_warns_once(tmp_path, conn, caplog) -> None:
    (tmp_path / "x").mkdir()
    expected = _go(_claude(tmp_path / "x", [INIT, RESULT], run_id=RUN))
    with caplog.at_level(logging.WARNING):
        got = _go(_claude(tmp_path, [INIT, RESULT], store=_Boom(), run_id=RUN))
    assert dict(got) == dict(expected) == {"ok": True}
    assert got.decision == expected.decision
    lines = [r.getMessage() for r in caplog.records if "store write failed" in r.getMessage()]
    assert len(lines) == 1 and lines[0].endswith(": disk full")


class _Stub:
    def __init__(self) -> None:
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any) -> Any:
        return SimpleNamespace(
            stop_reason="end_turn", model="claude-x-1", content=[SimpleNamespace(type="text", text='{"ok": true}')]
        )


def test_the_anthropic_runner_records_a_row_with_its_decision(conn) -> None:
    runner = AnthropicRunner(PROFILE, client=_Stub(), store=Store(conn), run_id=RUN)
    out = runner.run(role="r", tier="cheap", schema={}, prompt="p")
    row = _rows(conn)[0]
    assert (row[0], row[1], row[2], row[3]) == ("run-1", "p3-write", "r", 1)
    assert (row[5], row[6]) == (out.decision.model_id, out.decision.chosen_tier)


def test_the_anthropic_runner_records_a_failed_call_with_ok_zero(conn) -> None:
    class Refusing(_Stub):
        def _create(self, **kwargs: Any) -> Any:
            return SimpleNamespace(stop_reason="refusal", content=[])

    runner = AnthropicRunner(PROFILE, client=Refusing(), store=Store(conn), run_id=RUN)
    with pytest.raises(RunnerError):
        runner.run(role="r", schema={}, prompt="p")
    assert [r[3] for r in _rows(conn)] == [0]


def test_a_raising_store_does_not_change_the_anthropic_result_and_warns_once() -> None:
    plain = AnthropicRunner(PROFILE, client=_Stub()).run(role="r", schema={}, prompt="p")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        got = AnthropicRunner(PROFILE, client=_Stub(), store=_Boom(), run_id=RUN).run(role="r", schema={}, prompt="p")
    assert got == plain and got.decision == plain.decision
    assert [str(w.message).startswith("store write failed") for w in caught].count(True) == 1
