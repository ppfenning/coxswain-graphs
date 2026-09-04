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
