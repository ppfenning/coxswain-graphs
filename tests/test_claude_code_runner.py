"""The headless Claude Code runner, against a fake `claude` that records what it was asked.

No real Claude Code is invoked. A shell script standing in for the binary
writes its argv and stdin to a file and prints whatever JSON the test told it
to, so every assertion here is about the CONTRACT — which flags, which model,
which tools, what came back — and none of it needs a login.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from runner import RunnerError
from runner.claude_code_runner import ClaudeCodeRunner

PROFILE = {
    "profile": "fake-claude-code",
    "runner": "claude-code",
    "tiers": {"cheap": "haiku", "standard": "sonnet", "deep": "opus"},
    "tools": {
        "build": ["Read", "Grep", "Glob"],
        "work_item_arm": ["Read", "Write", "Edit"],
    },
}

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}


@pytest.fixture
def fake_claude(tmp_path: Path):
    """A stand-in binary. Returns (bin_path, record_path, set_output)."""
    record = tmp_path / "record.json"
    output = tmp_path / "output.json"
    helper = tmp_path / "record.py"
    helper.write_text(
        "import json, sys\n"
        f"json.dump({{'argv': sys.argv[1:], 'stdin': sys.stdin.read()}}, open({str(record)!r}, 'w'))\n",
        encoding="utf-8",
    )
    script = tmp_path / "claude"
    # stdin must pass straight through to the recorder — a heredoc here would
    # replace it, which is precisely the thing one of the tests checks.
    script.write_text(f"#!/bin/sh\npython3 {helper} \"$@\"\ncat {output}\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)

    def set_output(payload) -> None:
        output.write_text(json.dumps(payload), encoding="utf-8")

    set_output({"type": "result", "is_error": False, "structured_output": {"ok": True}, "total_cost_usd": 0.01, "num_turns": 1})
    return script, record, set_output


def runner_for(fake_claude, tmp_path: Path, **kwargs) -> ClaudeCodeRunner:
    script, _, _ = fake_claude
    return ClaudeCodeRunner(PROFILE, claude_bin=str(script), cwd=tmp_path, **kwargs)


def recorded(fake_claude) -> dict:
    _, record, _ = fake_claude
    return json.loads(record.read_text(encoding="utf-8"))


# ── the invocation ───────────────────────────────────────────────────────────


def test_tier_becomes_model_and_effort(fake_claude, tmp_path) -> None:
    runner = runner_for(fake_claude, tmp_path)
    runner.run(role="plan", tier="deep", schema=SCHEMA, prompt="go")
    argv = recorded(fake_claude)["argv"]
    assert argv[argv.index("--model") + 1] == "opus"
    assert argv[argv.index("--effort") + 1] == "xhigh"
    assert "-p" in argv and "--no-session-persistence" in argv
    assert argv[argv.index("--output-format") + 1] == "json"


def test_the_prompt_travels_on_stdin_and_the_schema_on_argv(fake_claude, tmp_path) -> None:
    runner = runner_for(fake_claude, tmp_path)
    runner.run(role="plan", schema=SCHEMA, prompt="the prompt, verbatim")
    rec = recorded(fake_claude)
    assert rec["stdin"] == "the prompt, verbatim"
    assert json.loads(rec["argv"][rec["argv"].index("--json-schema") + 1]) == SCHEMA


def test_a_role_without_a_grant_gets_no_tools(fake_claude, tmp_path) -> None:
    runner = runner_for(fake_claude, tmp_path)
    runner.run(role="plan", schema=SCHEMA, prompt="go")
    argv = recorded(fake_claude)["argv"]
    assert argv[-2:] == ["--tools", ""], "an ungranted role runs with no tools, like the API runner"
    assert "--permission-mode" not in argv


def test_a_granted_role_gets_exactly_its_tools_last(fake_claude, tmp_path) -> None:
    runner = runner_for(fake_claude, tmp_path)
    runner.run(role="build", schema=SCHEMA, prompt="go")
    argv = recorded(fake_claude)["argv"]
    assert argv[argv.index("--tools") + 1 :] == ["Read", "Grep", "Glob"]
    assert "--permission-mode" not in argv, "read-only tools need nothing accepted up front"


def test_an_arm_with_write_tools_has_edits_accepted(fake_claude, tmp_path) -> None:
    runner = runner_for(fake_claude, tmp_path)
    runner.run(role="work_item_arm", schema=SCHEMA, prompt="go")
    argv = recorded(fake_claude)["argv"]
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"


def test_the_skill_body_and_context_lead_the_system_prompt(fake_claude, tmp_path) -> None:
    body = tmp_path / "SKILL.md"
    body.write_text("# the craft\n", encoding="utf-8")
    pack = tmp_path / "conventions.md"
    pack.write_text("# the rules\n", encoding="utf-8")
    runner = runner_for(fake_claude, tmp_path, role_skills={"plan": str(body)})
    runner.run(role="plan", schema=SCHEMA, prompt="go", context=[str(pack)])
    argv = recorded(fake_claude)["argv"]
    system = argv[argv.index("--system-prompt") + 1]
    assert system.index("# the craft") < system.index("# the rules") < system.index("<workspace>")
    assert str(tmp_path) in system, "the node is told where the work store is"


def test_repo_dir_is_added_and_named(fake_claude, tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    runner = runner_for(fake_claude, tmp_path, repo_dir=repo)
    runner.run(role="build", schema=SCHEMA, prompt="go")
    argv = recorded(fake_claude)["argv"]
    assert argv[argv.index("--add-dir") + 1] == str(repo.resolve())
    assert str(repo.resolve()) in argv[argv.index("--system-prompt") + 1]


def test_repo_dir_is_a_plain_attribute_the_driver_may_move(fake_claude, tmp_path) -> None:
    runner = runner_for(fake_claude, tmp_path)
    runner.run(role="build", schema=SCHEMA, prompt="go")
    assert "--add-dir" not in recorded(fake_claude)["argv"]
    later = tmp_path / "phase-worktree"
    later.mkdir()
    runner.repo_dir = later
    runner.run(role="build", schema=SCHEMA, prompt="go")
    argv = recorded(fake_claude)["argv"]
    assert argv[argv.index("--add-dir") + 1] == str(later)


def test_a_missing_context_pack_is_named(fake_claude, tmp_path) -> None:
    runner = runner_for(fake_claude, tmp_path)
    with pytest.raises(RunnerError, match="context pack"):
        runner.run(role="plan", schema=SCHEMA, prompt="go", context=[str(tmp_path / "absent.md")])


# ── what comes back ──────────────────────────────────────────────────────────


def test_structured_output_is_the_result(fake_claude, tmp_path) -> None:
    runner = runner_for(fake_claude, tmp_path)
    assert dict(runner.run(role="plan", schema=SCHEMA, prompt="go")) == {"ok": True}
    assert runner.calls[-1]["model"] == "sonnet" and runner.calls[-1]["cost_usd"] == 0.01


def test_result_text_is_the_fallback_when_no_structured_output(fake_claude, tmp_path) -> None:
    _, _, set_output = fake_claude
    set_output({"is_error": False, "structured_output": None, "result": json.dumps({"ok": False})})
    runner = runner_for(fake_claude, tmp_path)
    assert dict(runner.run(role="plan", schema=SCHEMA, prompt="go")) == {"ok": False}


def test_claude_error_is_a_runner_error_carrying_the_reason(fake_claude, tmp_path) -> None:
    _, _, set_output = fake_claude
    set_output({"is_error": True, "result": "Not logged in · Please run /login"})
    runner = runner_for(fake_claude, tmp_path)
    with pytest.raises(RunnerError, match="Not logged in"):
        runner.run(role="plan", schema=SCHEMA, prompt="go")


def test_a_non_object_answer_is_refused(fake_claude, tmp_path) -> None:
    _, _, set_output = fake_claude
    set_output({"is_error": False, "structured_output": [1, 2, 3]})
    runner = runner_for(fake_claude, tmp_path)
    with pytest.raises(RunnerError, match="expected an object"):
        runner.run(role="plan", schema=SCHEMA, prompt="go")


def test_prose_with_no_structured_output_is_refused(fake_claude, tmp_path) -> None:
    _, _, set_output = fake_claude
    set_output({"is_error": False, "structured_output": None, "result": "I could not decide."})
    runner = runner_for(fake_claude, tmp_path)
    with pytest.raises(RunnerError, match="not JSON"):
        runner.run(role="plan", schema=SCHEMA, prompt="go")


def test_an_unknown_tier_is_named(fake_claude, tmp_path) -> None:
    runner = runner_for(fake_claude, tmp_path)
    with pytest.raises(RunnerError, match="no model for tier 'huge'"):
        runner.run(role="plan", tier="huge", schema=SCHEMA, prompt="go")


def test_a_missing_binary_is_named(tmp_path) -> None:
    runner = ClaudeCodeRunner(PROFILE, claude_bin=str(tmp_path / "nope"), cwd=tmp_path)
    with pytest.raises(RunnerError, match="not found"):
        runner.run(role="plan", schema=SCHEMA, prompt="go")


def test_a_profile_without_tiers_is_refused() -> None:
    with pytest.raises(RunnerError, match="no tiers"):
        ClaudeCodeRunner({"runner": "claude-code"})


# ── selection ────────────────────────────────────────────────────────────────


def test_build_runner_picks_this_runner_from_the_profile(tmp_path) -> None:
    from harness.runners import build_runner

    profile = tmp_path / "cc.yaml"
    profile.write_text("profile: cc\nrunner: claude-code\ntiers: {cheap: haiku, standard: sonnet, deep: opus}\n", encoding="utf-8")
    runner = build_runner(scripted=None, provider_profile=profile, workdir=tmp_path, repo=tmp_path / "r")
    assert isinstance(runner, ClaudeCodeRunner)
    assert runner.cwd == tmp_path.resolve()
    assert runner.repo_dir == (tmp_path / "r").resolve()


# ── isolation and cost ───────────────────────────────────────────────────────


def test_every_session_starts_with_no_mcp_servers_and_no_user_settings(fake_claude, tmp_path) -> None:
    """Measured: ~52k input tokens per node with the login's MCP schemas loaded, ~1k without."""
    runner = runner_for(fake_claude, tmp_path)
    runner.run(role="plan", schema=SCHEMA, prompt="go")
    argv = recorded(fake_claude)["argv"]
    assert "--strict-mcp-config" in argv
    assert json.loads(argv[argv.index("--mcp-config") + 1]) == {"mcpServers": {}}
    assert argv[argv.index("--setting-sources") + 1] == ""


def test_the_profile_may_set_effort_per_tier(fake_claude, tmp_path) -> None:
    script, _, _ = fake_claude
    runner = ClaudeCodeRunner({**PROFILE, "effort": {"deep": "high"}}, claude_bin=str(script), cwd=tmp_path)
    runner.run(role="plan", tier="deep", schema=SCHEMA, prompt="go")
    argv = recorded(fake_claude)["argv"]
    assert argv[argv.index("--effort") + 1] == "high"
    runner.run(role="plan", tier="cheap", schema=SCHEMA, prompt="go")
    assert recorded(fake_claude)["argv"][recorded(fake_claude)["argv"].index("--effort") + 1] == "low", "unset tiers keep the default"


def test_usage_is_recorded_per_call_and_summarised(fake_claude, tmp_path) -> None:
    from harness.usage import record_usage, summarize

    _, _, set_output = fake_claude
    set_output({"is_error": False, "structured_output": {"ok": True}, "total_cost_usd": 0.02, "num_turns": 3,
                "usage": {"input_tokens": 100, "cache_read_input_tokens": 900, "output_tokens": 50}})
    runner = runner_for(fake_claude, tmp_path)
    runner.run(role="plan", schema=SCHEMA, prompt="go")
    runner.run(role="review_charter", tier="deep", schema=SCHEMA, prompt="go")
    assert runner.calls[0]["input_tokens"] == 1000 and runner.calls[0]["output_tokens"] == 50
    summary = summarize(runner.calls)
    assert summary["calls"] == 2 and summary["cost_usd"] == 0.04 and summary["input_tokens"] == 2000
    assert set(summary["by_model"]) == {"sonnet", "opus"}
    out = record_usage(runner, runs_dir=tmp_path / "runs", run_id="r1")
    assert out == summary
    assert json.loads((tmp_path / "runs" / "r1.usage.json").read_text())["summary"]["calls"] == 2


def test_a_runner_with_nothing_to_count_records_nothing(tmp_path) -> None:
    from harness.usage import record_usage

    class Mute:
        pass

    assert record_usage(Mute(), runs_dir=tmp_path, run_id="r") is None
    assert not (tmp_path / "r.usage.json").exists()
