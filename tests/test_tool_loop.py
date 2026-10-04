"""The vendor-neutral tool loop, driven by a scripted adapter against a real git worktree."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from runner.protocol import BudgetStop
from runner.tool_loop import (
    TOOLS,
    AdapterStep,
    FinalAnswer,
    ToolCall,
    ToolError,
    _resolve_in_worktree,
    run_tool_loop,
)


class ScriptedAdapter:
    """Returns pre-programmed steps in order and keeps a snapshot of the messages each send saw."""

    def __init__(self, steps: list[AdapterStep], events: list[str] | None = None) -> None:
        self.steps = list(steps)
        self.seen: list[list[dict]] = []
        self.events = events

    def send(self, messages: list[dict], tools: list[dict]) -> AdapterStep:
        if self.events is not None:
            self.events.append("send")
        self.seen.append(list(messages))
        return self.steps.pop(0)


def call(name: str, **arguments: object) -> AdapterStep:
    return AdapterStep([ToolCall(f"c-{name}", name, dict(arguments))], None, {})


def done(patch: str = "", usage: dict | None = None) -> AdapterStep:
    return AdapterStep([], FinalAnswer({"patch": patch}), usage or {})


def git(cwd: Path, command: str) -> str:
    return subprocess.run(command, shell=True, cwd=cwd, capture_output=True, text=True, check=True).stdout


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    root = tmp_path / "wt"
    root.mkdir()
    (root / "a.txt").write_text("one\ntwo\nthree\n")
    git(root, "git init -q && git add -A && git -c user.name=t -c user.email=t@t commit -q -m init")
    return root


def run(adapter: ScriptedAdapter, worktree: Path, **kw: object):
    args = {"checks": [], "turn_cap": 5, "budget_usd": None, **kw}
    return run_tool_loop(adapter, worktree=worktree, system_prompt="sys", user_prompt="go", schema={}, **args)


def test_a_read_then_an_edit_then_a_final_answer_returns_the_worktrees_staged_diff(worktree: Path) -> None:
    adapter = ScriptedAdapter(
        [call("read_file", path="a.txt"), call("edit_file", path="a.txt", old="two", new="2"), done()]
    )
    result = run(adapter, worktree)
    assert result.stop_reason == "final"
    assert result.patch_error is None
    assert result.patch == git(worktree, "git add -A && git diff --cached")
    assert "+2" in result.patch
    assert adapter.seen[1][-1]["content"] == "one\ntwo\nthree\n"


def test_a_path_outside_the_worktree_gets_an_error_result_and_writes_nothing(worktree: Path) -> None:
    absolute = worktree.parent / "abs.txt"
    adapter = ScriptedAdapter(
        [
            call("write_file", path="../escape.txt", content="x"),
            call("write_file", path=str(absolute), content="x"),
            done(),
        ]
    )
    run(adapter, worktree)
    assert adapter.seen[1][-1]["is_error"] is True
    assert adapter.seen[2][-1]["is_error"] is True
    assert not (worktree.parent / "escape.txt").exists()
    assert not absolute.exists()


def test_a_check_missing_from_the_allowed_list_is_refused_and_never_runs(worktree: Path) -> None:
    adapter = ScriptedAdapter([call("run_check", command="touch sentinel.txt"), done()])
    run(adapter, worktree, checks=["true"])
    last = adapter.seen[1][-1]
    assert last["is_error"] is True
    assert "touch sentinel.txt" in last["content"]
    assert not (worktree / "sentinel.txt").exists()


def test_an_allowed_check_runs_and_reports_its_exit_code(worktree: Path) -> None:
    adapter = ScriptedAdapter([call("run_check", command="touch ran.txt"), done()])
    run(adapter, worktree, checks=["touch ran.txt"])
    assert adapter.seen[1][-1]["is_error"] is False
    assert "(exit 0)" in adapter.seen[1][-1]["content"]
    assert (worktree / "ran.txt").exists()


def test_wait_if_paused_runs_once_per_turn_before_each_send(worktree: Path) -> None:
    events: list[str] = []
    adapter = ScriptedAdapter([call("list_dir", path="."), done()], events)
    run(adapter, worktree, wait_if_paused=lambda: events.append("wait"))
    assert events == ["wait", "send", "wait", "send"]


def test_edit_file_refuses_a_missing_or_repeated_old_text_and_leaves_the_file(worktree: Path) -> None:
    (worktree / "b.txt").write_text("x x\n")
    adapter = ScriptedAdapter(
        [
            call("edit_file", path="b.txt", old="nope", new="y"),
            call("edit_file", path="b.txt", old="x", new="y"),
            done(),
        ]
    )
    run(adapter, worktree)
    assert "not found" in adapter.seen[1][-1]["content"]
    assert "not unique" in adapter.seen[2][-1]["content"]
    assert (worktree / "b.txt").read_text() == "x x\n"


def test_write_file_creates_parents_inside_the_worktree(worktree: Path) -> None:
    result = run(ScriptedAdapter([call("write_file", path="d/e/f.txt", content="hi"), done()]), worktree)
    assert (worktree / "d" / "e" / "f.txt").read_text() == "hi"
    assert "d/e/f.txt" in result.patch


def test_read_file_slices_lines_by_offset_and_limit(worktree: Path) -> None:
    adapter = ScriptedAdapter([call("read_file", path="a.txt", offset=1, limit=1), done()])
    run(adapter, worktree)
    assert adapter.seen[1][-1]["content"] == "two\n"


def test_resolve_accepts_the_worktree_and_paths_under_it_and_rejects_the_rest(worktree: Path) -> None:
    assert _resolve_in_worktree(worktree, ".") == worktree.resolve()
    assert _resolve_in_worktree(worktree, "a/b.txt") == worktree.resolve() / "a" / "b.txt"
    for bad in ("../x", "a/../../x", "/etc/passwd"):
        with pytest.raises(ToolError):
            _resolve_in_worktree(worktree, bad)


def test_the_turn_cap_stops_a_loop_that_never_answers(worktree: Path) -> None:
    adapter = ScriptedAdapter([call("list_dir", path="."), call("list_dir", path=".")])
    result = run(adapter, worktree, turn_cap=2)
    assert result.stop_reason == "turn_cap"
    assert adapter.steps == []


def test_usage_over_the_budget_raises_budget_stop_carrying_the_partial_patch(worktree: Path) -> None:
    write = AdapterStep(
        [ToolCall("w", "edit_file", {"path": "a.txt", "old": "one", "new": "1"})], None, {"cost_usd": 0.5}
    )
    adapter = ScriptedAdapter([write, AdapterStep([], None, {"cost_usd": 0.7})])
    with pytest.raises(BudgetStop) as stop:
        run(adapter, worktree, budget_usd=1.0)
    assert stop.value.spent_usd == pytest.approx(1.2)
    assert stop.value.num_turns == 2
    assert "+1" in stop.value.partial_patch


def test_usage_totals_are_summed_across_steps(worktree: Path) -> None:
    step = AdapterStep([ToolCall("l", "list_dir", {"path": "."})], None, {"cost_usd": 0.1, "output_tokens": 5})
    result = run(ScriptedAdapter([step, done(usage={"cost_usd": 0.2, "output_tokens": 7})]), worktree)
    assert result.usage["output_tokens"] == 12
    assert result.usage["cost_usd"] == pytest.approx(0.3)


def test_the_tool_definitions_name_the_five_tools() -> None:
    assert {t["name"]: t["parameters"]["required"] for t in TOOLS} == {
        "read_file": ["path"],
        "list_dir": ["path"],
        "write_file": ["path", "content"],
        "edit_file": ["path", "old", "new"],
        "run_check": ["command"],
    }
