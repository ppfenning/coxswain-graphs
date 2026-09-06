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
from runner.claude_code_runner import ClaudeCodeRunner, next_spent
from runner.protocol import BudgetStop
from runner.scripted import ScriptedRunner

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
    runner.run(role="review_charter", schema=SCHEMA, prompt="go")
    argv = recorded(fake_claude)["argv"]
    assert argv[argv.index("--add-dir") + 1] == str(repo.resolve())
    assert str(repo.resolve()) in argv[argv.index("--system-prompt") + 1]


def test_repo_dir_is_a_plain_attribute_the_driver_may_move(fake_claude, tmp_path) -> None:
    runner = runner_for(fake_claude, tmp_path)
    runner.run(role="review_charter", schema=SCHEMA, prompt="go")
    assert "--add-dir" not in recorded(fake_claude)["argv"]
    later = tmp_path / "phase-worktree"
    later.mkdir()
    runner.repo_dir = later
    runner.run(role="review_charter", schema=SCHEMA, prompt="go")
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
    call = runner.calls[0]
    assert (call["input_tokens"], call["cache_read_tokens"], call["input_total"], call["output_tokens"]) == (100, 900, 1000, 50)
    summary = summarize(runner.calls)
    assert summary["calls"] == 2 and summary["cost_usd"] == 0.04 and summary["input_total"] == 2000
    assert summary["cache_read_tokens"] == 1800, "the split survives the summary"
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


def test_the_node_is_told_patches_are_not_applied_in_the_checkout(fake_claude, tmp_path) -> None:
    """Handoff once quarantined every task for 'no changes present in the repo'."""
    repo = tmp_path / "repo"
    repo.mkdir()
    runner = runner_for(fake_claude, tmp_path, repo_dir=repo)
    runner.run(role="handoff", schema=SCHEMA, prompt="go")
    system = recorded(fake_claude)["argv"]
    assert "NOT applied in that checkout" in system[system.index("--system-prompt") + 1]


def test_a_claude_error_names_its_subtype(fake_claude, tmp_path) -> None:
    _, _, set_output = fake_claude
    set_output({"is_error": True, "subtype": "error_max_turns", "result": None, "num_turns": 40})
    runner = runner_for(fake_claude, tmp_path)
    with pytest.raises(RunnerError, match="error_max_turns"):
        runner.run(role="build", schema=SCHEMA, prompt="go")


# ── the builder's scratch worktree ───────────────────────────────────────────


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real one-commit git repository, so worktree operations are real."""
    import subprocess as sp

    root = tmp_path / "target"
    root.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}
    sp.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    (root / "f.txt").write_text("one\n", encoding="utf-8")
    sp.run(["git", "-C", str(root), "add", "-A"], check=True, env=env)
    sp.run(["git", "-C", str(root), "commit", "-qm", "init"], check=True, env=env)
    return root


def test_the_builder_gets_a_scratch_worktree_and_runs_in_it(fake_claude, tmp_path, repo) -> None:
    runner = runner_for(fake_claude, tmp_path, repo_dir=repo)
    runner.tools["build"] = ["Read", "Write", "Edit", "Bash"]
    runner.run(role="build", schema=SCHEMA, prompt="go")
    argv = recorded(fake_claude)["argv"]
    system = argv[argv.index("--system-prompt") + 1]
    assert "scratch checkout" in system and "git add -A && git diff --cached" in system
    assert "VERBATIM" in system, "the patch is transcribed from git, never authored"
    scratch = [argv[i + 1] for i, a in enumerate(argv) if a == "--add-dir" and "agent-graphs-build-" in argv[i + 1]]
    assert scratch, "the scratch is readable by the node"


def test_the_scratch_is_removed_afterwards(fake_claude, tmp_path, repo) -> None:
    import subprocess as sp

    runner = runner_for(fake_claude, tmp_path, repo_dir=repo)
    runner.tools["build"] = ["Read", "Write", "Edit", "Bash"]
    runner.run(role="build", schema=SCHEMA, prompt="go")
    argv = recorded(fake_claude)["argv"]
    scratch = next(argv[i + 1] for i, a in enumerate(argv) if a == "--add-dir" and "agent-graphs-build-" in argv[i + 1])
    assert not Path(scratch).exists()
    listed = sp.run(["git", "-C", str(repo), "worktree", "list"], capture_output=True, text=True).stdout
    assert "agent-graphs-build-" not in listed, "no worktree is left registered"


def test_two_builders_never_share_a_scratch(fake_claude, tmp_path, repo) -> None:
    runner = runner_for(fake_claude, tmp_path, repo_dir=repo)
    runner.tools["build"] = ["Read", "Write", "Edit", "Bash"]
    seen = []
    for _ in range(2):
        runner.run(role="build", schema=SCHEMA, prompt="go")
        argv = recorded(fake_claude)["argv"]
        seen.append(next(argv[i + 1] for i, a in enumerate(argv) if a == "--add-dir" and "agent-graphs-build-" in argv[i + 1]))
    assert seen[0] != seen[1], "parallel tasks in one phase must not edit the same tree"


def test_a_reading_role_gets_no_scratch(fake_claude, tmp_path, repo) -> None:
    runner = runner_for(fake_claude, tmp_path, repo_dir=repo)
    runner.run(role="review_charter", schema=SCHEMA, prompt="go")
    argv = recorded(fake_claude)["argv"]
    assert "agent-graphs-build-" not in " ".join(argv)
    assert "scratch checkout" not in argv[argv.index("--system-prompt") + 1]
    assert "never edit it" in argv[argv.index("--system-prompt") + 1]


def test_without_a_target_repo_there_is_no_scratch(fake_claude, tmp_path) -> None:
    runner = runner_for(fake_claude, tmp_path)
    runner.tools["build"] = ["Read", "Write", "Edit", "Bash"]
    runner.run(role="build", schema=SCHEMA, prompt="go")
    assert "agent-graphs-build-" not in " ".join(recorded(fake_claude)["argv"])


def test_a_builder_pointed_at_a_non_repository_fails_loudly(fake_claude, tmp_path) -> None:
    """No git, no computed diff. Say so rather than let it hand-write one."""
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    runner = runner_for(fake_claude, tmp_path, repo_dir=not_a_repo)
    runner.tools["build"] = ["Read", "Write", "Edit", "Bash"]
    with pytest.raises(RunnerError, match="scratch worktree"):
        runner.run(role="build", schema=SCHEMA, prompt="go")


# ── cost ceilings, tier overrides, the digest ────────────────────────────────


def test_a_tier_budget_becomes_a_dollar_ceiling(fake_claude, tmp_path) -> None:
    script, _, _ = fake_claude
    runner = ClaudeCodeRunner({**PROFILE, "budget_usd": {"standard": 0.35}}, claude_bin=str(script), cwd=tmp_path)
    runner.run(role="plan", schema=SCHEMA, prompt="go")
    argv = recorded(fake_claude)["argv"]
    assert argv[argv.index("--max-budget-usd") + 1] == "0.3500"


def test_a_role_budget_beats_its_tier(fake_claude, tmp_path) -> None:
    script, _, _ = fake_claude
    runner = ClaudeCodeRunner(
        {**PROFILE, "budget_usd": {"standard": 0.35}, "role_budget_usd": {"build": 0.6}}, claude_bin=str(script), cwd=tmp_path
    )
    runner.run(role="build", schema=SCHEMA, prompt="go")
    argv = recorded(fake_claude)["argv"]
    assert argv[argv.index("--max-budget-usd") + 1] == "0.6000"


def test_no_budget_means_no_ceiling(fake_claude, tmp_path) -> None:
    runner = runner_for(fake_claude, tmp_path)
    runner.run(role="plan", schema=SCHEMA, prompt="go")
    assert "--max-budget-usd" not in recorded(fake_claude)["argv"]


def test_a_call_level_budget_overrides_the_role_ceiling(fake_claude, tmp_path) -> None:
    script, _, _ = fake_claude
    runner = ClaudeCodeRunner(
        {**PROFILE, "budget_usd": {"standard": 0.35}, "role_budget_usd": {"build": 0.6}}, claude_bin=str(script), cwd=tmp_path
    )
    runner.run(role="build", schema=SCHEMA, prompt="go", budget_usd=2.5)
    argv = recorded(fake_claude)["argv"]
    assert argv[argv.index("--max-budget-usd") + 1] == "2.5000"


def test_the_scripted_runner_records_the_budget_override() -> None:
    runner = ScriptedRunner({"plan": {"ok": True}})
    runner.run(role="plan", tier="standard", schema=SCHEMA, prompt="go", budget_usd=1.25)
    assert runner.calls[0]["budget_usd"] == 1.25


def test_a_profile_may_reassign_a_roles_tier(fake_claude, tmp_path) -> None:
    script, _, _ = fake_claude
    runner = ClaudeCodeRunner({**PROFILE, "tier_overrides": {"scope_epic": "cheap"}}, claude_bin=str(script), cwd=tmp_path)
    runner.run(role="scope_epic", tier="standard", schema=SCHEMA, prompt="go")
    argv = recorded(fake_claude)["argv"]
    assert argv[argv.index("--model") + 1] == "haiku" and argv[argv.index("--effort") + 1] == "low"
    assert runner.calls[-1]["tier"] == "cheap", "the record says what actually ran"


def test_over_budget_is_named(fake_claude, tmp_path) -> None:
    _, _, set_output = fake_claude
    set_output({"is_error": True, "subtype": "error_max_budget_usd", "result": None})
    runner = runner_for(fake_claude, tmp_path)
    with pytest.raises(RunnerError, match="error_max_budget_usd"):
        runner.run(role="build", schema=SCHEMA, prompt="go")


def test_the_digest_reaches_roles_with_tools_and_nobody_else(fake_claude, tmp_path, repo) -> None:
    runner = runner_for(fake_claude, tmp_path, repo_dir=repo)
    runner.tools["build"] = ["Read", "Write", "Edit", "Bash"]
    runner.repo_digest = "2 tracked files\nf.txt (1)"
    runner.run(role="build", schema=SCHEMA, prompt="go")
    system = recorded(fake_claude)["argv"]
    system = system[system.index("--system-prompt") + 1]
    assert "<repo-digest>" in system and "f.txt (1)" in system
    assert "as few turns as you can" in system, "the builder is told why turns cost"
    runner.run(role="handoff", schema=SCHEMA, prompt="go")
    system = recorded(fake_claude)["argv"]
    system = system[system.index("--system-prompt") + 1]
    assert "<repo-digest>" in system, "reading roles with a repo see the map too"
    runner.repo_dir = None
    runner.run(role="handoff", schema=SCHEMA, prompt="go")
    system = recorded(fake_claude)["argv"]
    system = system[system.index("--system-prompt") + 1]
    assert "<repo-digest>" not in system, "no repository, no map"


# ── tracing, paths, ranged reads ─────────────────────────────────────────────


def test_a_trace_dir_switches_to_stream_json_and_keeps_every_event(fake_claude, tmp_path) -> None:
    script, _, set_output = fake_claude
    events = [
        {"type": "system", "subtype": "init"},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "/r/f.py"}}]}},
        {"type": "user", "message": {"content": []}},
        {"type": "result", "subtype": "success", "is_error": False, "structured_output": {"ok": True},
         "num_turns": 2, "total_cost_usd": 0.02, "usage": {"input_tokens": 10, "cache_read_input_tokens": 90, "output_tokens": 5}},
    ]
    (tmp_path / "output.json").write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    runner = ClaudeCodeRunner(PROFILE, claude_bin=str(script), cwd=tmp_path, trace_dir=tmp_path / "trace")
    out = runner.run(role="build", schema=SCHEMA, prompt="go")
    argv = recorded(fake_claude)["argv"]
    assert argv[argv.index("--output-format") + 1] == "stream-json" and "--verbose" in argv
    assert dict(out) == {"ok": True}
    trace = tmp_path / "trace" / "build-1.jsonl"
    assert trace.is_file() and len(trace.read_text().splitlines()) == 4
    assert runner.calls[-1]["trace"] == str(trace) and runner.calls[-1]["turns"] == 2
    runner.run(role="build", schema=SCHEMA, prompt="again")
    assert (tmp_path / "trace" / "build-2.jsonl").is_file(), "one file per call, numbered per role"


def test_a_stream_with_no_result_event_is_a_named_failure(fake_claude, tmp_path) -> None:
    script, _, _ = fake_claude
    (tmp_path / "output.json").write_text(json.dumps({"type": "system"}) + "\n", encoding="utf-8")
    runner = ClaudeCodeRunner(PROFILE, claude_bin=str(script), cwd=tmp_path, trace_dir=tmp_path / "trace")
    with pytest.raises(RunnerError, match="no result event"):
        runner.run(role="plan", schema=SCHEMA, prompt="go")


def test_without_a_trace_dir_nothing_changes(fake_claude, tmp_path) -> None:
    runner = runner_for(fake_claude, tmp_path)
    runner.run(role="plan", schema=SCHEMA, prompt="go")
    argv = recorded(fake_claude)["argv"]
    assert argv[argv.index("--output-format") + 1] == "json" and "--verbose" not in argv
    assert "trace" not in runner.calls[-1]


def test_nodes_are_told_to_use_absolute_paths_and_ranged_reads(fake_claude, tmp_path, repo) -> None:
    runner = runner_for(fake_claude, tmp_path, repo_dir=repo)
    runner.run(role="review_charter", schema=SCHEMA, prompt="go")
    argv = recorded(fake_claude)["argv"]
    system = argv[argv.index("--system-prompt") + 1]
    assert f"ABSOLUTE paths under {repo.resolve()}" in system
    assert "offset and limit" in system and "300" in system
    runner.tools["build"] = ["Read", "Write", "Edit", "Bash"]
    runner.run(role="build", schema=SCHEMA, prompt="go")
    argv = recorded(fake_claude)["argv"]
    system = argv[argv.index("--system-prompt") + 1]
    scratch = next(argv[i + 1] for i, a in enumerate(argv) if a == "--add-dir" and "agent-graphs-build-" in argv[i + 1])
    assert f"ABSOLUTE paths under {scratch}" in system, "the builder's paths point at its scratch, not the shared tree"


def test_the_builder_is_handed_the_projects_check_commands_verbatim(fake_claude, tmp_path, repo) -> None:
    """Traced builds spent a third of their turns discovering how to run the tests."""
    runner = runner_for(fake_claude, tmp_path, repo_dir=repo)
    runner.tools["build"] = ["Read", "Write", "Edit", "Bash"]
    runner.check_commands = ["pytest -q", "ruff check ."]
    runner.run(role="build", schema=SCHEMA, prompt="go")
    argv = recorded(fake_claude)["argv"]
    system = argv[argv.index("--system-prompt") + 1]
    assert "exactly: `pytest -q; ruff check .`" in system
    assert "wasted turn" in system
    runner.run(role="review_charter", schema=SCHEMA, prompt="go")
    argv = recorded(fake_claude)["argv"]
    assert "exactly: `pytest" not in argv[argv.index("--system-prompt") + 1], "only the builder runs anything"


def test_bash_is_pre_approved_for_the_checks_and_git_and_nothing_else(fake_claude, tmp_path, repo) -> None:
    """acceptEdits never covered Bash: seven epics of builds never ran a test."""
    runner = runner_for(fake_claude, tmp_path, repo_dir=repo)
    runner.tools["build"] = ["Read", "Write", "Edit", "Bash"]
    runner.check_commands = ["pytest -q", "ruff check ."]
    runner.run(role="build", schema=SCHEMA, prompt="go")
    argv = recorded(fake_claude)["argv"]
    i = argv.index("--allowedTools")
    allowed = argv[i + 1 : argv.index("--tools")]
    assert allowed == ["Bash(pytest:*)", "Bash(ruff:*)", "Bash(git status:*)", "Bash(git diff:*)", "Bash(git add:*)"]
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits", "edits are still accepted up front"


def test_no_bash_no_allowlist(fake_claude, tmp_path, repo) -> None:
    runner = runner_for(fake_claude, tmp_path, repo_dir=repo)
    runner.check_commands = ["pytest -q"]
    runner.run(role="build", schema=SCHEMA, prompt="go")  # PROFILE grants build Read/Grep/Glob only
    assert "--allowedTools" not in recorded(fake_claude)["argv"]


# ── threads: one session and one scratch across plan, build and retry ────────


def _add_dirs(argv):
    return [argv[i + 1] for i, a in enumerate(argv) if a == "--add-dir"]


def test_a_thread_is_one_session_resumed_and_one_scratch_kept(fake_claude, tmp_path, repo) -> None:
    runner = runner_for(fake_claude, tmp_path, repo_dir=repo)
    runner.tools["build"] = ["Read", "Write", "Edit", "Bash"]
    runner.run(role="plan", schema=SCHEMA, prompt="plan it", thread="T-1")
    first = recorded(fake_claude)["argv"]
    assert "--no-session-persistence" not in first, "a thread persists so it can be resumed"
    sid = first[first.index("--session-id") + 1]
    scratch = next(d for d in _add_dirs(first) if "agent-graphs-build-" in d)
    assert Path(scratch).is_dir(), "the scratch outlives the call"
    system = first[first.index("--system-prompt") + 1]
    assert "your own checkout" in system and "scratch checkout" not in system, "the planner reads; it does not patch"

    runner.run(role="build", schema=SCHEMA, prompt="build it", thread="T-1")
    second = recorded(fake_claude)["argv"]
    assert second[second.index("--resume") + 1] == sid and "--session-id" not in second
    assert scratch in _add_dirs(second), "the builder edits the tree the planner read"
    assert "scratch checkout" in second[second.index("--system-prompt") + 1]

    runner.run(role="build", schema=SCHEMA, prompt="retry", thread="T-1")
    third = recorded(fake_claude)["argv"]
    assert third[third.index("--resume") + 1] == sid and scratch in _add_dirs(third)

    runner.close()
    assert not Path(scratch).exists(), "closed threads leave nothing behind"


def test_threads_never_share_a_session_or_a_tree(fake_claude, tmp_path, repo) -> None:
    runner = runner_for(fake_claude, tmp_path, repo_dir=repo)
    runner.run(role="plan", schema=SCHEMA, prompt="a", thread="T-A")
    a = recorded(fake_claude)["argv"]
    runner.run(role="plan", schema=SCHEMA, prompt="b", thread="T-B")
    b = recorded(fake_claude)["argv"]
    assert a[a.index("--session-id") + 1] != b[b.index("--session-id") + 1]
    assert set(_add_dirs(a)) != set(_add_dirs(b))
    runner.close()


def test_a_call_without_a_thread_is_unchanged(fake_claude, tmp_path, repo) -> None:
    runner = runner_for(fake_claude, tmp_path, repo_dir=repo)
    runner.run(role="review_charter", schema=SCHEMA, prompt="go")
    argv = recorded(fake_claude)["argv"]
    assert "--no-session-persistence" in argv and "--session-id" not in argv and "--resume" not in argv


# ── one retry, for an error about the call rather than about the work ───────

SAFEGUARD = {
    "type": "result",
    "is_error": True,
    "subtype": "[reasoning_extraction]",
    "result": "Opus 5's safeguards flagged this message",
    "num_turns": 1,
}
REFUSED = {
    "type": "result",
    "is_error": True,
    "subtype": "error_max_budget_usd",
    "result": "the session reached its budget ceiling",
    "num_turns": 24,
    "total_cost_usd": 0.97,
}
OK = {"type": "result", "is_error": False, "structured_output": {"ok": True}, "total_cost_usd": 0.02, "num_turns": 3}


@pytest.fixture
def sequenced_claude(tmp_path: Path):
    """A stand-in that answers differently on each call.

    Returns (bin_path, set_sequence, calls). The last entry repeats, so a test
    only has to script the answers it cares about. Every call's argv is also
    appended, one line per call, to `tmp_path / "argvs.txt"` — the same idea as
    `recorded(fake_claude)`, kept out of the returned tuple so the existing
    unpacking here is unchanged.
    """
    outputs = tmp_path / "outputs.json"
    counter = tmp_path / "calls"
    argv_log = tmp_path / "argvs.jsonl"
    helper = tmp_path / "sequence.py"
    helper.write_text(
        "import json, pathlib\n"
        f"c = pathlib.Path({str(counter)!r}); n = int(c.read_text() or 0) if c.exists() else 0\n"
        "c.write_text(str(n + 1))\n"
        f"outs = json.load(open({str(outputs)!r}))\n"
        "print(json.dumps(outs[min(n, len(outs) - 1)]))\n",
        encoding="utf-8",
    )
    # A system prompt carries newlines, so a shell `echo "$@"` would split one
    # call across several lines. A tiny helper writes one JSON array per call
    # instead — the same move `record.py` above makes for `fake_claude`.
    argv_helper = tmp_path / "record_argv.py"
    argv_helper.write_text(
        "import json, sys\n"
        f"open({str(argv_log)!r}, 'a').write(json.dumps(sys.argv[1:]) + chr(10))\n",
        encoding="utf-8",
    )
    script = tmp_path / "claude"
    script.write_text(
        f"#!/bin/sh\npython3 {argv_helper} \"$@\"\ncat >/dev/null\npython3 {helper}\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)

    def set_sequence(*payloads) -> None:
        outputs.write_text(json.dumps(list(payloads)), encoding="utf-8")

    def calls() -> int:
        return int(counter.read_text()) if counter.exists() else 0

    set_sequence(OK)
    return script, set_sequence, calls


def test_a_safeguard_error_is_retried_once_and_the_node_returns(sequenced_claude, tmp_path) -> None:
    """It hit arbitrate in two runs and quarantined a finished task each time."""
    script, set_sequence, calls = sequenced_claude
    set_sequence(SAFEGUARD, OK)
    runner = ClaudeCodeRunner(PROFILE, claude_bin=str(script), cwd=tmp_path)

    assert dict(runner.run(role="arbitrate", schema=SCHEMA, prompt="decide")) == {"ok": True}
    assert calls() == 2, "asked twice, not more"
    assert len(runner.calls) == 1, "the failed attempt is not billed as a node result"


def test_a_safeguard_error_twice_is_still_a_failure(sequenced_claude, tmp_path) -> None:
    """One retry, never a loop. A budget disappears in loops like that."""
    script, set_sequence, calls = sequenced_claude
    set_sequence(SAFEGUARD)
    runner = ClaudeCodeRunner(PROFILE, claude_bin=str(script), cwd=tmp_path)

    with pytest.raises(RunnerError, match="safeguards flagged"):
        runner.run(role="arbitrate", schema=SCHEMA, prompt="decide")
    assert calls() == 2


def test_an_error_about_the_work_is_not_retried(sequenced_claude, tmp_path) -> None:
    """A budget stop is a real answer about a real session; asking again spends again."""
    script, set_sequence, calls = sequenced_claude
    set_sequence(REFUSED, OK)
    runner = ClaudeCodeRunner(PROFILE, claude_bin=str(script), cwd=tmp_path)

    with pytest.raises(RunnerError, match="error_max_budget_usd"):
        runner.run(role="build", schema=SCHEMA, prompt="build it")
    assert calls() == 1


def test_a_budget_stop_on_a_thread_carries_the_session_and_spend(sequenced_claude, tmp_path) -> None:
    script, set_sequence, _ = sequenced_claude
    argv_log = tmp_path / "argvs.jsonl"
    set_sequence(REFUSED)
    runner = ClaudeCodeRunner(PROFILE, claude_bin=str(script), cwd=tmp_path)

    with pytest.raises(BudgetStop) as exc_info:
        runner.run(role="build", schema=SCHEMA, prompt="build it", thread="T")
    argv = json.loads(argv_log.read_text(encoding="utf-8").splitlines()[0])
    stop = exc_info.value
    assert stop.role == "build"
    assert stop.session == argv[argv.index("--session-id") + 1]
    assert stop.spent_usd == 0.97
    assert isinstance(stop, RunnerError)


def test_a_budget_stop_without_a_thread_has_no_session(sequenced_claude, tmp_path) -> None:
    script, set_sequence, _ = sequenced_claude
    set_sequence(REFUSED)
    runner = ClaudeCodeRunner(PROFILE, claude_bin=str(script), cwd=tmp_path)

    with pytest.raises(BudgetStop) as exc_info:
        runner.run(role="build", schema=SCHEMA, prompt="build it")
    assert exc_info.value.session is None


def test_a_threaded_budget_stop_keeps_the_thread_for_a_resume(sequenced_claude, tmp_path, repo) -> None:
    script, set_sequence, _ = sequenced_claude
    argv_log = tmp_path / "argvs.jsonl"
    set_sequence(OK, REFUSED, OK)
    runner = ClaudeCodeRunner(PROFILE, claude_bin=str(script), cwd=tmp_path, repo_dir=repo)

    runner.run(role="build", schema=SCHEMA, prompt="build it", thread="T")
    with pytest.raises(BudgetStop):
        runner.run(role="build", schema=SCHEMA, prompt="build more", thread="T")

    assert "T" in runner._threads, "the stopped session is not discarded"
    assert runner._threads["T"]["calls"] == 1, "the failed call never advanced the counter"
    assert runner._threads["T"]["spent_usd"] == 0.97
    assert Path(runner._threads["T"]["scratch"]).is_dir(), "the half-written tree is kept"

    runner.run(role="build", schema=SCHEMA, prompt="retry", thread="T")
    calls_argv = [json.loads(line) for line in argv_log.read_text(encoding="utf-8").splitlines()]
    sid = calls_argv[0][calls_argv[0].index("--session-id") + 1]
    third = calls_argv[2]
    assert third[third.index("--resume") + 1] == sid, "the next call resumes the same session"


def test_a_resumed_thread_sends_spent_plus_the_ceiling(sequenced_claude, tmp_path, repo) -> None:
    script, set_sequence, _ = sequenced_claude
    argv_log = tmp_path / "argvs.jsonl"
    set_sequence(OK, REFUSED, OK)
    profile = {**PROFILE, "budget_usd": {"standard": 1.0}}
    runner = ClaudeCodeRunner(profile, claude_bin=str(script), cwd=tmp_path, repo_dir=repo)

    runner.run(role="build", schema=SCHEMA, prompt="build it", thread="T")
    with pytest.raises(BudgetStop):
        runner.run(role="build", schema=SCHEMA, prompt="build more", thread="T")
    runner.run(role="build", schema=SCHEMA, prompt="retry", thread="T")

    third = json.loads(argv_log.read_text(encoding="utf-8").splitlines()[2])
    assert third[third.index("--max-budget-usd") + 1] == "1.9700", "0.97 spent plus the 1.00 ceiling"


def test_an_explicit_budget_on_the_resume_is_added_to_spent_too(sequenced_claude, tmp_path, repo) -> None:
    script, set_sequence, _ = sequenced_claude
    argv_log = tmp_path / "argvs.jsonl"
    set_sequence(OK, REFUSED, OK)
    profile = {**PROFILE, "budget_usd": {"standard": 1.0}}
    runner = ClaudeCodeRunner(profile, claude_bin=str(script), cwd=tmp_path, repo_dir=repo)

    runner.run(role="build", schema=SCHEMA, prompt="build it", thread="T")
    with pytest.raises(BudgetStop):
        runner.run(role="build", schema=SCHEMA, prompt="build more", thread="T")
    runner.run(role="build", schema=SCHEMA, prompt="retry", thread="T", budget_usd=2.5)

    third = json.loads(argv_log.read_text(encoding="utf-8").splitlines()[2])
    assert third[third.index("--max-budget-usd") + 1] == "3.4700", "0.97 spent plus the 2.50 override"


def test_next_spent_accumulates_across_successes() -> None:
    assert next_spent(next_spent(0.0, 0.02, stopped=False), 0.03, stopped=False) == 0.05


def test_next_spent_on_a_stop_replaces_rather_than_adds() -> None:
    assert next_spent(0.5, 0.97, stopped=True) == 0.97


def test_a_successful_resume_keeps_accumulating_spend(sequenced_claude, tmp_path, repo) -> None:
    script, set_sequence, _ = sequenced_claude
    argv_log = tmp_path / "argvs.jsonl"
    set_sequence(OK, REFUSED, OK, OK)
    profile = {**PROFILE, "budget_usd": {"standard": 1.0}}
    runner = ClaudeCodeRunner(profile, claude_bin=str(script), cwd=tmp_path, repo_dir=repo)

    runner.run(role="build", schema=SCHEMA, prompt="build it", thread="T")
    with pytest.raises(BudgetStop):
        runner.run(role="build", schema=SCHEMA, prompt="build more", thread="T")
    runner.run(role="build", schema=SCHEMA, prompt="retry", thread="T")
    runner.run(role="build", schema=SCHEMA, prompt="again", thread="T")

    fourth = json.loads(argv_log.read_text(encoding="utf-8").splitlines()[3])
    assert fourth[fourth.index("--max-budget-usd") + 1] == "1.9900", \
        "0.97 stop replaces call 1's 0.02, call 3's 0.02 success adds to reach 0.99, plus the 1.00 ceiling"


def test_a_threaded_budget_stop_carries_the_scratch_as_a_partial_patch(sequenced_claude, tmp_path, repo) -> None:
    script, set_sequence, _ = sequenced_claude
    script.write_text(
        script.read_text().replace("#!/bin/sh\n", "#!/bin/sh\nprintf 'half a change\\n' > half-written.txt\n"),
        encoding="utf-8",
    )
    set_sequence(REFUSED)
    runner = ClaudeCodeRunner(PROFILE, claude_bin=str(script), cwd=tmp_path, repo_dir=repo)

    with pytest.raises(BudgetStop) as exc_info:
        runner.run(role="build", schema=SCHEMA, prompt="build it", thread="T")
    assert "half-written.txt" in exc_info.value.partial_patch
    assert "+half a change" in exc_info.value.partial_patch


def test_a_threadless_budget_stop_drops_the_scratch_and_has_no_partial_patch(sequenced_claude, tmp_path, repo) -> None:
    script, set_sequence, _ = sequenced_claude
    argv_log = tmp_path / "argvs.jsonl"
    set_sequence(REFUSED)
    runner = ClaudeCodeRunner(PROFILE, claude_bin=str(script), cwd=tmp_path, repo_dir=repo)

    with pytest.raises(BudgetStop) as exc_info:
        runner.run(role="build", schema=SCHEMA, prompt="build it")
    argv = json.loads(argv_log.read_text(encoding="utf-8").splitlines()[0])
    scratch = next(d for d in _add_dirs(argv) if "agent-graphs-build-" in d)
    assert not Path(scratch).exists(), "a threadless scratch is dropped as today"
    assert exc_info.value.partial_patch == ""


def test_a_non_budget_error_is_a_runner_error_not_a_budget_stop(sequenced_claude, tmp_path) -> None:
    script, set_sequence, _ = sequenced_claude
    set_sequence(SAFEGUARD)
    runner = ClaudeCodeRunner(PROFILE, claude_bin=str(script), cwd=tmp_path)

    with pytest.raises(RunnerError) as exc_info:
        runner.run(role="arbitrate", schema=SCHEMA, prompt="decide")
    assert not isinstance(exc_info.value, BudgetStop)


def test_the_failed_attempt_keeps_its_trace(sequenced_claude, tmp_path) -> None:
    """A transient error nobody can count later is one nobody can fix."""
    script, set_sequence, _ = sequenced_claude
    set_sequence({**SAFEGUARD, "type": "result"}, OK)
    traces = tmp_path / "traces"
    runner = ClaudeCodeRunner(PROFILE, claude_bin=str(script), cwd=tmp_path, trace_dir=traces)

    runner.run(role="arbitrate", schema=SCHEMA, prompt="decide")
    written = sorted(p.name for p in traces.glob("*.jsonl"))
    assert written == ["arbitrate-1.error.jsonl", "arbitrate-1.jsonl"]


def test_a_transient_failure_on_a_thread_retries_the_same_session_id(sequenced_claude, tmp_path) -> None:
    """The first call on a thread has no session the CLI is known to have kept.

    A retry that sent `--resume` there would be asking to resume a session
    that may never have been created, and would fail for a different reason
    than the attempt it was meant to repeat.
    """
    script, set_sequence, _ = sequenced_claude
    argv_log = tmp_path / "argvs.jsonl"
    set_sequence(SAFEGUARD, OK)
    runner = ClaudeCodeRunner(PROFILE, claude_bin=str(script), cwd=tmp_path)

    assert dict(runner.run(role="build", schema=SCHEMA, prompt="build it", thread="T")) == {"ok": True}
    calls = [json.loads(line) for line in argv_log.read_text(encoding="utf-8").splitlines()]
    assert len(calls) == 2, "the failed attempt and its retry, nothing more"
    assert all("--session-id" in argv and "--resume" not in argv for argv in calls), \
        "both attempts of the first call ask for the same not-yet-confirmed session"

    set_sequence(OK)
    runner.run(role="build", schema=SCHEMA, prompt="again", thread="T")
    assert runner._threads["T"]["spent_usd"] == 0.04, "the failed attempt left the total untouched, only two successes counted"
    third = [json.loads(line) for line in argv_log.read_text(encoding="utf-8").splitlines()][2]
    assert "--resume" in third and "--session-id" not in third, \
        "the counter only advanced once a call actually succeeded"


def test_a_retry_resends_the_same_budget_override(sequenced_claude, tmp_path) -> None:
    script, set_sequence, _ = sequenced_claude
    argv_log = tmp_path / "argvs.jsonl"
    set_sequence(SAFEGUARD, OK)
    runner = ClaudeCodeRunner(PROFILE, claude_bin=str(script), cwd=tmp_path)

    runner.run(role="arbitrate", schema=SCHEMA, prompt="decide", budget_usd=2.5)
    calls = [json.loads(line) for line in argv_log.read_text(encoding="utf-8").splitlines()]
    assert len(calls) == 2
    assert all(argv[argv.index("--max-budget-usd") + 1] == "2.5000" for argv in calls)


# ── the sandbox boundary is told to the node, not discovered by hitting it ──


def _system_prompt(record_path: Path) -> str:
    argv = json.loads(record_path.read_text())["argv"]
    return argv[argv.index("--system-prompt") + 1]


def test_the_builder_is_told_exactly_which_commands_it_may_run(fake_claude, tmp_path, repo) -> None:
    """Run 21's build was complete and was refused for evidence it was not allowed to produce."""
    _, record, _ = fake_claude
    runner = runner_for(fake_claude, tmp_path, repo_dir=repo)
    runner.tools["build"] = ["Read", "Write", "Edit", "Bash"]
    runner.check_commands = ["pytest -q"]

    runner.run(role="build", schema=SCHEMA, prompt="build it")
    system = _system_prompt(record)

    assert "`pytest`" in system and "`git status`" in system
    assert "refused by the sandbox before it runs" in system
    assert "a refusal is not evidence" in system


def test_the_permitted_list_is_the_list_that_is_enforced(fake_claude, tmp_path, repo) -> None:
    """One source. A prompt naming a different set than `--allowedTools` leaves
    the boundary something the builder still has to find by hitting it."""
    _, record, _ = fake_claude
    runner = runner_for(fake_claude, tmp_path, repo_dir=repo)
    runner.tools["build"] = ["Read", "Bash"]
    runner.check_commands = ["uv run pytest", "ruff check"]

    runner.run(role="build", schema=SCHEMA, prompt="build it")
    argv = json.loads(record.read_text())["argv"]
    enforced = argv[argv.index("--allowedTools") + 1 : argv.index("--tools")]
    system = _system_prompt(record)

    assert enforced == ["Bash(uv:*)", "Bash(ruff:*)", "Bash(git status:*)", "Bash(git diff:*)", "Bash(git add:*)"]
    for name in enforced:
        assert f"`{name[len('Bash('):-len(':*)')]}`" in system
