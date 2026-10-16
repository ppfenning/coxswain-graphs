"""OpenAICompatibleRunner builds through the tool loop when it has a worktree, against a scripted server."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from runner.openai_compatible_runner import OpenAICompatibleRunner, OpenAICompatibleToolLoopAdapter
from runner.protocol import BudgetStop
from runner.tool_loop import TOOLS, ToolCall, _CheckClock, _execute
from tests.fake_openai_server import FakeOpenAIServer

ENV_VAR = "LOCAL_LLM_URL"
PROFILE = {
    "endpoint_env": ENV_VAR,
    "capabilities": {"structured_output": False},
    "tiers": {"cheap": "local/qwen-small", "standard": "local/qwen-big", "deep": "local/qwen-big"},
}
SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
PRICED = {"qwen-small": {"input": 10000.0, "output": 10000.0}}


def _tool_reply(call_id: str, name: str, arguments: dict) -> tuple[int, dict]:
    message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}
        ],
    }
    return 200, {
        "choices": [{"message": message, "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2},
    }


def _worktree(root: Path) -> Path:
    tree = root / "tree"
    tree.mkdir()
    (tree / "a.txt").write_text("hello\n")
    for cmd in (
        "git init -q",
        "git config user.email t@example.com",
        "git config user.name t",
        "git add -A",
        "git commit -q -m init",
    ):
        subprocess.run(cmd, shell=True, cwd=tree, check=True)
    return tree


def _runner(server: FakeOpenAIServer, tree: Path, **profile: object) -> OpenAICompatibleRunner:
    return OpenAICompatibleRunner({**PROFILE, **profile}, role_skills={}, env={ENV_VAR: server.base_url}, cwd=tree)


def test_a_tool_calls_entry_round_trips_into_a_tool_call_the_loop_can_execute(tmp_path: Path) -> None:
    tree = _worktree(tmp_path)
    with FakeOpenAIServer([_tool_reply("c1", "read_file", {"path": "a.txt"})]) as server:
        adapter = OpenAICompatibleToolLoopAdapter(
            server.base_url, model="qwen-small", max_tokens=64, timeout=10.0, prices={}
        )
        messages = [{"role": "system", "content": "be brief", "schema": SCHEMA}, {"role": "user", "content": "go"}]
        step = adapter.send(messages, TOOLS)
    assert step.tool_calls == [ToolCall("c1", "read_file", {"path": "a.txt"})]
    assert step.final is None
    body = server.requests[0]["body"]
    assert body["tools"][0] == {
        "type": "function",
        "function": {"name": "read_file", "description": TOOLS[0]["description"], "parameters": TOOLS[0]["parameters"]},
    }
    assert _execute(step.tool_calls[0], tree, (), _CheckClock()).content == "hello\n"


def test_a_build_call_with_a_worktree_returns_the_worktrees_staged_diff_as_its_patch(tmp_path: Path) -> None:
    tree = _worktree(tmp_path)
    replies = [
        _tool_reply("c1", "read_file", {"path": "a.txt"}),
        _tool_reply("c2", "edit_file", {"path": "a.txt", "old": "hello", "new": "world"}),
        '{"ok": true}',
    ]
    with FakeOpenAIServer(replies) as server:
        result = _runner(server, tree).run(role="build", tier="cheap", schema=SCHEMA, prompt="edit a.txt", thread="t")
    staged = subprocess.run("git add -A && git diff --cached", shell=True, cwd=tree, capture_output=True, text=True)
    assert "+world" in result["patch"]
    assert result["patch"] == staged.stdout
    assert result["ok"] is True
    assert len(server.requests) == 3


def test_a_build_call_on_a_thread_with_nothing_left_stops_before_any_request(tmp_path: Path) -> None:
    tree = _worktree(tmp_path)
    with FakeOpenAIServer([]) as server:
        runner = _runner(server, tree, budget_usd=0.01, prices=PRICED)
        runner._spent["t"] = 0.01
        with pytest.raises(BudgetStop):
            runner.run(role="build", tier="cheap", schema=SCHEMA, prompt="go", thread="t", budget_usd=5.0)
    assert server.requests == []


def test_a_per_call_budget_does_not_replace_the_profiles_and_the_spend_is_kept(tmp_path: Path) -> None:
    tree = _worktree(tmp_path)
    replies = [_tool_reply("c1", "list_dir", {"path": "."}), _tool_reply("c2", "list_dir", {"path": "."})]
    with FakeOpenAIServer(replies) as server:
        runner = _runner(server, tree, budget_usd=0.06, prices=PRICED)
        with pytest.raises(BudgetStop):
            runner.run(role="build", tier="cheap", schema=SCHEMA, prompt="go", thread="t", budget_usd=5.0)
    assert len(server.requests) == 2
    assert runner._spent["t"] == pytest.approx(0.10)
