from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from runner import anthropic_runner
from runner.anthropic_runner import AnthropicRunner, AnthropicToolLoopAdapter, _to_anthropic
from runner.protocol import LimitStop, RunnerError
from runner.tool_loop import ToolCall, run_tool_loop

PROFILE = {"tiers": {"cheap": "m-cheap", "standard": "m-std", "deep": "m-deep"}}
# $1000 per million tokens: one scripted turn of 10 in and 5 out costs $0.015.
PRICED = {**PROFILE, "prices": {"m-std": {"input": 1000.0, "output": 1000.0}}}
SCHEMA = {"type": "object", "required": ["summary"], "properties": {"summary": {"type": "string"}}}
FINAL = '{"summary": "done"}'


def _text(text: str) -> Any:
    return SimpleNamespace(type="text", text=text)


def _use(id: str, name: str, **arguments: Any) -> Any:
    return SimpleNamespace(type="tool_use", id=id, name=name, input=arguments)


class _Scripted:
    """A Messages-API client that answers each `create` with the next scripted reply and keeps the requests."""

    def __init__(self, *replies: list[Any]) -> None:
        self.calls: list[dict[str, Any]] = []
        self._replies = list(replies)
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        reply = self._replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(stop_reason="end_turn", content=reply, usage=SimpleNamespace(input_tokens=10, output_tokens=5))


def _repo(path: Path) -> Path:
    path.mkdir()
    (path / "a.txt").write_text("hello\n")
    for cmd in ("git init -q", "git add -A", "git -c user.name=t -c user.email=t@t commit -qm init"):
        subprocess.run(cmd, shell=True, cwd=path, check=True)
    return path


def _staged_diff(path: Path) -> str:
    return subprocess.run("git add -A && git diff --cached", shell=True, cwd=path, capture_output=True, text=True).stdout


def test_a_tool_use_block_becomes_a_tool_call_the_loop_executes(tmp_path):
    tree = _repo(tmp_path / "tree")
    client = _Scripted(
        [_use("tu_1", "read_file", path="a.txt")], [_use("tu_2", "read_file", path="missing.txt")], [_text(FINAL)]
    )
    adapter = AnthropicToolLoopAdapter(client, model="m", max_tokens=100, effort="high", prices={})
    steps: list[Any] = []
    recorder = SimpleNamespace(send=lambda messages, tools: steps.append(adapter.send(messages, tools)) or steps[-1])
    run_tool_loop(
        recorder, worktree=tree, system_prompt="sys", user_prompt="go", schema=SCHEMA, checks=(), turn_cap=5, budget_usd=None
    )
    assert steps[0].tool_calls == [ToolCall("tu_1", "read_file", {"path": "a.txt"})]
    assert client.calls[1]["messages"][-1]["content"] == [
        {"type": "tool_result", "tool_use_id": "tu_1", "content": "hello\n", "is_error": False}
    ]
    assert client.calls[2]["messages"][-1]["content"][0]["is_error"] is True


def test_a_build_with_cwd_returns_the_worktree_diff_as_its_patch(tmp_path):
    tree = _repo(tmp_path / "tree")
    client = _Scripted(
        [_use("tu_1", "read_file", path="a.txt")],
        [_use("tu_2", "edit_file", path="a.txt", old="hello", new="goodbye")],
        [_text(FINAL)],
    )
    result = AnthropicRunner(PROFILE, client=client, cwd=tree).run(role="build", schema=SCHEMA, prompt="go")
    assert result["summary"] == "done"
    assert result["patch"] == _staged_diff(tree)
    assert "+goodbye" in result["patch"]


def test_a_build_that_uses_every_turn_without_answering_is_an_error_and_is_charged(tmp_path, monkeypatch):
    monkeypatch.setattr(anthropic_runner, "_BUILD_TURN_CAP", 2)
    client = _Scripted([_use("tu_1", "read_file", path="a.txt")], [_use("tu_2", "read_file", path="a.txt")])
    runner = AnthropicRunner(PRICED, client=client, cwd=_repo(tmp_path / "tree"))
    with pytest.raises(RunnerError, match="without a final answer"):
        runner.run(role="build", schema=SCHEMA, prompt="go", thread="t")
    assert runner._thread_spend["t"] == pytest.approx(0.03)


def test_a_limit_mid_loop_still_charges_the_turns_already_billed(tmp_path):
    limit = RuntimeError("overloaded")
    limit.status_code = 529  # type: ignore[attr-defined]
    client = _Scripted([_use("tu_1", "read_file", path="a.txt")], limit)
    runner = AnthropicRunner(PRICED, client=client, cwd=_repo(tmp_path / "tree"))
    with pytest.raises(LimitStop):
        runner.run(role="build", schema=SCHEMA, prompt="go", thread="t")
    assert runner._thread_spend["t"] == pytest.approx(0.015)


def test_a_budgeted_build_on_an_unpriced_model_is_refused_before_any_request(tmp_path):
    client = _Scripted([_text(FINAL)])
    runner = AnthropicRunner(PROFILE, client=client, cwd=_repo(tmp_path / "tree"))
    with pytest.raises(RunnerError, match="no price"):
        runner.run(role="build", schema=SCHEMA, prompt="go", budget_usd=1.0)
    assert client.calls == []


def test_a_build_without_cwd_makes_one_stateless_request():
    client = _Scripted([_text(FINAL)])
    result = AnthropicRunner(PROFILE, client=client).run(role="build", schema=SCHEMA, prompt="go")
    assert result == {"summary": "done"}
    assert len(client.calls) == 1
    assert "tools" not in client.calls[0]


def test_tool_results_for_one_turn_share_one_user_message():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "go"},
        {"role": "assistant", "tool_calls": [{"id": "1", "name": "list_dir", "arguments": {"path": "."}}]},
        {"role": "tool", "tool_call_id": "1", "content": "a.txt", "is_error": False},
        {"role": "tool", "tool_call_id": "2", "content": "boom", "is_error": True},
    ]
    request = _to_anthropic(messages, [{"name": "n", "description": "d", "parameters": {"type": "object"}}])
    assert request["system"] == "sys"
    assert request["tools"] == [{"name": "n", "description": "d", "input_schema": {"type": "object"}}]
    assert request["messages"][2]["content"] == [
        {"type": "tool_result", "tool_use_id": "1", "content": "a.txt", "is_error": False},
        {"type": "tool_result", "tool_use_id": "2", "content": "boom", "is_error": True},
    ]
