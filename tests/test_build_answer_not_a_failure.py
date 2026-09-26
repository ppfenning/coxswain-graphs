"""A successful build answer reaches the graph; it is never an exception message.

The cause: the runner took any result text mentioning "usage limit" for the
account's limit banner, so a build whose patch touched limit handling became
`LimitStop(detail=<the whole answer>)` and a quarantine reason. The runner is
driven through a fake `claude` binary, as in `tests/test_claude_code_runner.py`.
The scripted runner cannot reproduce it: it never reaches the runner's parsing.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from graphs._spec import GraphSpec
from harness import Invocation, invoke_graphs
from runner import RunnerError
from runner.claude_code_runner import ClaudeCodeRunner
from runner.protocol import LimitStop

PROFILE = {
    "profile": "fake-claude-code",
    "runner": "claude-code",
    "tiers": {"cheap": "haiku", "standard": "sonnet", "deep": "opus"},
    "tools": {"build": ["Read"]},
}

PATCH = (
    "diff --git a/runner/limit.py b/runner/limit.py\n"
    "--- a/runner/limit.py\n"
    "+++ b/runner/limit.py\n"
    "@@ -1,2 +1,3 @@\n"
    "-BANNER = 'usage limit'\n"
    "+BANNER = 'usage limit reached'\n"
    "+PAUSE = True\n"
    " context\n"
)

ANSWER = {
    "patch": PATCH,
    "summary": "pause the run on the usage limit banner",
    "files_touched": ["runner/limit.py"],
    "commands_run": [{"command": "pytest -q", "output": "1 passed"}],
}

SCHEMA = {"type": "object"}
BANNER = "You've hit your session limit · resets 10:50am (America/New_York)"


def build_with(tmp_path: Path, payload: dict) -> dict:
    """Run one `build` call against a fake `claude` that prints `payload`."""
    output = tmp_path / "output.json"
    output.write_text(json.dumps(payload), encoding="utf-8")
    script = tmp_path / "claude"
    script.write_text(f"#!/bin/sh\ncat {output}\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    runner = ClaudeCodeRunner(PROFILE, claude_bin=str(script), cwd=tmp_path)
    return runner.run(role="build", schema=SCHEMA, prompt="go")


def result_event(**fields) -> dict:
    return {"type": "result", "is_error": False, "total_cost_usd": 0.01, "num_turns": 1, **fields}


def test_a_structured_answer_that_mentions_the_usage_limit_is_returned(tmp_path) -> None:
    payload = result_event(structured_output=ANSWER, result="Done: pause on the usage limit banner.")
    assert build_with(tmp_path, payload) == ANSWER


def test_an_answer_arriving_as_result_text_that_mentions_the_usage_limit_is_returned(tmp_path) -> None:
    payload = result_event(structured_output=None, result=json.dumps(ANSWER))
    assert build_with(tmp_path, payload) == ANSWER


def test_the_real_banner_still_pauses_the_run(tmp_path) -> None:
    with pytest.raises(LimitStop) as caught:
        build_with(tmp_path, result_event(structured_output=None, result=BANNER))
    assert str(caught.value) == BANNER


def test_a_failure_string_is_cut_to_its_first_line_when_long_or_an_object() -> None:
    from harness.invoke import _bounded

    assert _bounded("plain and short") == "plain and short"
    assert _bounded('{\n  "patch": "diff"\n}') == "{ (message truncated)"
    assert _bounded("first line\n" + "x" * 2001) == "first line (message truncated)"
    assert _bounded('{"patch": "' + "x" * 300) == '{"patch": "' + "x" * 189 + " (message truncated)"


def test_invoke_graphs_quarantines_a_failure_under_the_guard() -> None:
    def graph(args, runner):
        raise RunnerError(json.dumps({"patch": PATCH * 100}))

    specs = {"g": GraphSpec(name="g", graph_name="g-propose", run=graph)}
    _, _, failures = invoke_graphs(
        [Invocation(id="t1", graph="g", args={})], specs=specs, runner=None, run_id="r", max_parallel=1
    )
    assert [len(f) for f in failures] == [len("t1: ") + 200 + len(" (message truncated)")]
    assert failures[0].endswith("(message truncated)")
