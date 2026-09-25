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
import yaml

from harness.store_migrate import open_store
from harness.store_read import calls as store_calls
from harness.store_write import Store
from runner import RunnerError
from runner.claude_code_runner import (
    ClaudeCodeRunner,
    _alt_model_for,
    _init_facts,
    _parse_version,
    _run_verify,
    _version_violations,
    apply_reported_patch,
    files_touched_from_patch,
    is_safeguard_refusal,
    next_spent,
    reconcile_patch,
    redirect_paths,
    self_reported_commands,
    trace_commands,
)
from runner.decision_log import RouterDecision, to_row
from runner.protocol import BudgetStop, Capability, ProviderProfile, resolve_profile
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
        "import json, sys, pathlib\n"
        f"json.dump({{'argv': sys.argv[1:], 'stdin': sys.stdin.read()}}, open({str(record)!r}, 'w'))\n"
        "if pathlib.Path('.git').exists():\n"
        "    pathlib.Path('built.txt').write_text('edited\\n')\n",
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


@pytest.fixture
def conn():
    c = open_store("sqlite:///:memory:", "2026-09-24T00:00:00Z")
    yield c
    c.close()


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


def test_a_task_kwarg_is_stamped_onto_the_call_as_task_id(fake_claude, tmp_path) -> None:
    runner = runner_for(fake_claude, tmp_path)
    runner.run(role="build", schema=SCHEMA, prompt="go", task="t1-probe")
    assert runner.calls[-1]["task_id"] == "t1-probe"


def test_no_task_kwarg_stamps_task_id_none(fake_claude, tmp_path) -> None:
    runner = runner_for(fake_claude, tmp_path)
    runner.run(role="build", schema=SCHEMA, prompt="go")
    assert runner.calls[-1]["task_id"] is None


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


def test_the_runner_declares_the_full_capability_set() -> None:
    runner = ClaudeCodeRunner(PROFILE)
    assert set(runner.capabilities) == {c.value for c in Capability}
    assert runner.capabilities["resume"] is True


def test_a_role_needing_resume_resolves_on_claude_code_with_no_fallback() -> None:
    profile = ProviderProfile(capabilities=ClaudeCodeRunner(PROFILE).capabilities, tiers=PROFILE["tiers"])
    resolved, fallback = resolve_profile(profile, role="build", tier="cheap", required=[Capability.RESUME])
    assert resolved is profile
    assert fallback is None


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


def test_a_successful_call_records_its_decision_row_in_calls(fake_claude, tmp_path) -> None:
    runner = runner_for(fake_claude, tmp_path)
    result = runner.run(role="plan", schema=SCHEMA, prompt="go")
    assert runner.calls[-1]["decision"] == to_row(result.decision)
    assert runner.calls[-1]["decision"]["role"] == "plan"


def test_a_shadow_tagged_decision_reaches_the_ledger_and_usage_json(fake_claude, tmp_path, conn) -> None:
    from harness.runners import _DelegatingFastPath
    from harness.usage import record_usage
    from runner.system_one import Answer, Noul, RoleSetting, RoleSpec

    class Decider:
        def decide(self, question, state):
            return Answer("noul", "yes", {"yes": 0.9}, 0.9)

    spec = RoleSpec(build=lambda req: (Noul("ok?"), {}), render=lambda a: {"ok": True}, agrees=lambda a, r: r["ok"] is True)
    inner = runner_for(fake_claude, tmp_path, runs_dir=tmp_path / "runs", run_id="r1", store=Store(conn))
    fast = _DelegatingFastPath(inner, Decider(), {"plan": RoleSetting("shadow", 0.8)}, {"plan": spec}, backend="jev-1.13.0")
    fast.run(role="plan", schema=SCHEMA, prompt="go")
    fast.run(role="plan", schema=SCHEMA, prompt="go")
    assert inner.tag_decision is None, "the hook is cleared after each call"
    ledger = _ledger_lines(conn, "r1")
    assert [row["decision"]["system_one_agreed"] for row in ledger] == [True, True]
    record_usage(fast, runs_dir=tmp_path / "runs", run_id="r1")
    calls = json.loads((tmp_path / "runs" / "r1.usage.json").read_text())["calls"]
    assert [(c["decision"]["system_one_agreed"], c["decision"]["system_one_backend"]) for c in calls] == [(True, "jev-1.13.0")] * 2


def test_overlapping_shadow_calls_on_a_shared_runner_each_keep_their_own_tag(fake_claude, tmp_path, conn) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from harness.runners import _DelegatingFastPath
    from harness.usage import record_usage
    from runner.system_one import Answer, Noul, RoleSetting, RoleSpec

    class Decider:
        def decide(self, question, state):
            return Answer("noul", "yes", {"yes": 0.9}, 0.9)

    def spec(agrees: bool) -> RoleSpec:
        return RoleSpec(build=lambda req: (Noul("ok?"), {}), render=lambda a: {"ok": True}, agrees=lambda a, r: agrees)

    # Each fake claude waits until both have started, so the two calls overlap for certain.
    starts = tmp_path / "starts"
    starts.mkdir()
    real, _, _ = fake_claude
    slow = tmp_path / "slow-claude"
    slow.write_text(
        f"#!/bin/sh\ntouch {starts}/$$\nn=0\n"
        f"while [ $(ls {starts} | wc -l) -lt 2 ] && [ $n -lt 200 ]; do sleep 0.05; n=$((n+1)); done\n"
        f"exec {real} \"$@\"\n",
        encoding="utf-8",
    )
    slow.chmod(slow.stat().st_mode | stat.S_IXUSR)
    inner = ClaudeCodeRunner(
        PROFILE, claude_bin=str(slow), cwd=tmp_path, runs_dir=tmp_path / "runs", run_id="r1", store=Store(conn)
    )
    settings = {"plan": RoleSetting("shadow", 0.8), "review": RoleSetting("shadow", 0.8)}
    fast = _DelegatingFastPath(inner, Decider(), settings, {"plan": spec(True), "review": spec(False)}, backend="jev-1.13.0")
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {role: pool.submit(fast.run, role=role, schema=SCHEMA, prompt="go") for role in ("plan", "review")}
        results = {role: future.result() for role, future in futures.items()}
    assert len(list(starts.iterdir())) == 2
    assert {role: r.decision.system_one_agreed for role, r in results.items()} == {"plan": True, "review": False}
    ledger = _ledger_lines(conn, "r1")
    assert {row["role"]: row["decision"]["system_one_agreed"] for row in ledger} == {"plan": True, "review": False}
    record_usage(fast, runs_dir=tmp_path / "runs", run_id="r1")
    calls = json.loads((tmp_path / "runs" / "r1.usage.json").read_text())["calls"]
    assert {c["role"]: c["decision"]["system_one_agreed"] for c in calls} == {"plan": True, "review": False}


def test_the_tag_hook_is_per_thread(fake_claude, tmp_path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    runner = runner_for(fake_claude, tmp_path)
    runner.tag_decision = lambda result: None
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(lambda: runner.tag_decision).result() is None
    assert runner.tag_decision is not None


def test_a_raising_tag_hook_records_the_untagged_decision(fake_claude, tmp_path) -> None:
    runner = runner_for(fake_claude, tmp_path)

    def boom(result):
        raise RuntimeError("tagger down")

    runner.tag_decision = boom
    result = runner.run(role="plan", schema=SCHEMA, prompt="go")
    assert runner.calls[-1]["decision"]["system_one_agreed"] is None
    assert result.decision.role == "plan"


def test_a_failed_call_records_no_decision_row(fake_claude, tmp_path) -> None:
    _, _, set_output = fake_claude
    set_output({"is_error": True, "result": "Not logged in"})
    runner = runner_for(fake_claude, tmp_path)
    with pytest.raises(RunnerError):
        runner.run(role="plan", schema=SCHEMA, prompt="go")
    assert all("decision" not in call for call in runner.calls)


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
    assert "Read files with the Read tool" in system and "`verify:` commands" in system
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


# ── the patch is computed from the scratch, not recalled ────────────────────


def test_reconcile_patch_prefers_the_scratchs_diff_over_a_differing_report() -> None:
    assert reconcile_patch("diff --git a/x\n-a\n+b\n", "diff --git a/y\n-c\n+d\n") == ("diff --git a/y\n-c\n+d\n", None)


def test_reconcile_patch_is_patch_outside_scratch_when_the_scratch_is_clean_but_the_model_reported_something() -> None:
    assert reconcile_patch("diff --git a/x\n-a\n+b\n", "") == ("", "patch_outside_scratch")


def test_reconcile_patch_is_patch_empty_when_both_are_empty() -> None:
    assert reconcile_patch("", "") == ("", "patch_empty")


def test_reconcile_patch_passes_through_an_identical_report() -> None:
    assert reconcile_patch("same\n", "same\n") == ("same\n", None)


def test_a_successful_build_returns_the_scratchs_diff_not_the_models(fake_claude, tmp_path, repo) -> None:
    _, _, set_output = fake_claude
    set_output({"is_error": False, "total_cost_usd": 0.01, "num_turns": 1,
                "structured_output": {"summary": "did the thing", "files_touched": ["f.txt"],
                                      "commands_run": [], "patch": "diff --git a/test.txt\n-a\n+b\n"}})
    runner = runner_for(fake_claude, tmp_path, repo_dir=repo)
    runner.tools["build"] = ["Read", "Write", "Edit", "Bash"]
    out = runner.run(role="build", schema=SCHEMA, prompt="go")
    assert "built.txt" in out["patch"] and "test.txt" not in out["patch"]
    assert out["summary"] == "did the thing" and out["files_touched"] == ["f.txt"]


def test_the_recorded_calls_files_touched_comes_from_the_scratchs_diff_not_the_model(fake_claude, tmp_path, repo) -> None:
    _, _, set_output = fake_claude
    set_output({"is_error": False, "total_cost_usd": 0.01, "num_turns": 1,
                "structured_output": {"files_touched": ["f.txt"], "patch": "diff --git a/test.txt\n-a\n+b\n"}})
    runner = runner_for(fake_claude, tmp_path, repo_dir=repo)
    runner.tools["build"] = ["Read", "Write", "Edit", "Bash"]
    runner.run(role="build", schema=SCHEMA, prompt="go")
    assert runner.calls[-1]["files_touched"] == ["built.txt"], "derived from the reconciled patch, not the model's report"


def test_a_build_that_edited_nothing_but_reported_a_patch_is_refused_as_patch_outside_scratch(tmp_path, repo) -> None:
    """A bare `cat` of the canned output: no `.git` write, so the scratch stays clean."""
    output = tmp_path / "output.json"
    output.write_text(
        json.dumps({"is_error": False, "total_cost_usd": 0.01, "num_turns": 1,
                    "structured_output": {"patch": "diff --git a/test.txt\n-a\n+b\n"}}),
        encoding="utf-8",
    )
    script = tmp_path / "claude"
    script.write_text(f"#!/bin/sh\ncat {output}\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    runner = ClaudeCodeRunner(PROFILE, claude_bin=str(script), cwd=tmp_path, repo_dir=repo)
    with pytest.raises(RunnerError, match="patch_outside_scratch"):
        runner.run(role="build", schema=SCHEMA, prompt="go")


def _reporting_claude(tmp_path: Path, patch: str) -> Path:
    """A stand-in that edits nothing and reports `patch`, recording its argv and stdin."""
    output = tmp_path / "output.json"
    output.write_text(
        json.dumps({"is_error": False, "total_cost_usd": 0.01, "num_turns": 1, "structured_output": {"patch": patch}}),
        encoding="utf-8",
    )
    helper = tmp_path / "record.py"
    helper.write_text(
        "import json, sys\n"
        f"json.dump({{'argv': sys.argv[1:], 'stdin': sys.stdin.read()}}, open({str(tmp_path / 'record.json')!r}, 'w'))\n",
        encoding="utf-8",
    )
    script = tmp_path / "claude"
    script.write_text(f"#!/bin/sh\npython3 {helper} \"$@\"\ncat {output}\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


F_PATCH = "diff --git a/f.txt b/f.txt\n--- a/f.txt\n+++ b/f.txt\n@@ -1 +1 @@\n-one\n+two\n"


def test_a_clean_scratch_with_an_applicable_reported_patch_is_recovered_and_marked_reported(tmp_path, repo) -> None:
    script = _reporting_claude(tmp_path, F_PATCH)
    runner = ClaudeCodeRunner(PROFILE, claude_bin=str(script), cwd=tmp_path, repo_dir=repo)
    result = runner.run(role="build", schema=SCHEMA, prompt="go")
    assert "+two" in result["patch"]
    assert runner.calls[-1]["patch_source"] == "reported"
    assert runner.calls[-1]["files_touched"] == ["f.txt"]


def test_a_scratch_edit_is_marked_computed(fake_claude, tmp_path, repo) -> None:
    runner = runner_for(fake_claude, tmp_path, repo_dir=repo)
    runner.run(role="build", schema=SCHEMA, prompt="go")
    assert runner.calls[-1]["patch_source"] == "computed"


def test_run_verify_returns_a_row_per_command_with_output_and_exit_code_and_never_raises(tmp_path) -> None:
    rows = _run_verify(tmp_path, ["echo one", "false"])
    assert rows[0] == {"command": "echo one", "output": "one\n(exit 0)", "source": "harness_verify"}
    assert rows[1]["output"].endswith("(exit 1)") and rows[1]["source"] == "harness_verify"
    assert _run_verify(tmp_path, []) == []
    assert _run_verify(tmp_path / "missing", ["echo x"])[0]["output"].startswith("could not start")


def test_run_verify_names_a_timeout_instead_of_raising(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("runner.claude_code_runner._VERIFY_TIMEOUT_S", 0.2)
    assert _run_verify(tmp_path, ["sleep 5"])[0]["output"] == "timed out after 0.2s"


def test_run_verify_skips_commands_once_the_total_budget_is_spent(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("runner.claude_code_runner._VERIFY_TOTAL_S", 0.3)
    rows = _run_verify(tmp_path, ["sleep 5", "echo never"])
    assert rows[0]["output"].startswith("timed out after 0.")
    assert rows[1] == {"command": "echo never", "output": "skipped: the 0.3s verify budget is spent", "source": "harness_verify"}


def test_a_build_that_will_fail_reconciliation_is_not_verified(tmp_path, repo) -> None:
    """A clean scratch with a reported patch that does not apply raises next, so no verify command should run."""
    marker = tmp_path / "ran"
    script = _reporting_claude(tmp_path, F_PATCH.replace("-one", "-absent"))
    runner = ClaudeCodeRunner(PROFILE, claude_bin=str(script), cwd=tmp_path, repo_dir=repo, runs_dir=tmp_path, run_id="r1")
    runner.verify_by_task = {"t1": [f"touch {marker}"]}
    with pytest.raises(RunnerError, match="patch_outside_scratch"):
        runner.run(role="build", schema=SCHEMA, prompt="go", task="t1")
    assert not marker.exists()


def test_a_build_with_verify_commands_gets_their_rows_after_the_builders_own(fake_claude, tmp_path, repo) -> None:
    _, _, set_output = fake_claude
    set_output({"is_error": False, "total_cost_usd": 0.01, "num_turns": 1,
                "structured_output": {"summary": "s", "commands_run": ["pytest -q"], "patch": ""}})
    runner = runner_for(fake_claude, tmp_path, repo_dir=repo)
    runner.verify_by_task = {"t1": ["cat built.txt"]}
    out = runner.run(role="build", schema=SCHEMA, prompt="go", task="t1")
    assert out["commands_run"] == [
        "pytest -q",
        {"command": "cat built.txt", "output": "edited\n(exit 0)", "source": "harness_verify"},
    ]
    assert runner.calls[-1]["commands_run"] == [{"command": "pytest -q", "source": "self_report"}]


def test_a_threaded_build_runs_its_verify_commands_in_the_thread_scratch(fake_claude, tmp_path, repo) -> None:
    runner = runner_for(fake_claude, tmp_path, repo_dir=repo)
    runner.verify_by_task = {"t1": ["cat built.txt"]}
    out = runner.run(role="build", schema=SCHEMA, prompt="go", thread="T-1", task="t1")
    assert out["commands_run"][-1]["output"] == "edited\n(exit 0)"


def test_a_verify_command_that_writes_leaves_nothing_in_the_threads_scratch_for_the_next_attempt(fake_claude, tmp_path, repo) -> None:
    runner = runner_for(fake_claude, tmp_path, repo_dir=repo)
    runner.verify_by_task = {"t1": ["echo junk > artifact.txt", "echo more >> built.txt", "echo x > staged.txt && git add staged.txt"]}
    first = runner.run(role="build", schema=SCHEMA, prompt="go", thread="T-1", task="t1")
    scratch = runner._threads["T-1"]["scratch"]
    assert [row["output"] for row in first["commands_run"]][-1].endswith("(exit 0)")
    assert not (scratch / "artifact.txt").exists() and not (scratch / "staged.txt").exists()
    assert (scratch / "built.txt").read_text(encoding="utf-8") == "edited\n"
    second = runner.run(role="build", schema=SCHEMA, prompt="again", thread="T-1", task="t1")
    assert "built.txt" in second["patch"]
    assert not any(name in second["patch"] for name in ("artifact.txt", "staged.txt", "more"))


def test_a_verify_run_is_reported_when_the_scratch_cannot_be_restored(fake_claude, tmp_path) -> None:
    runner = runner_for(fake_claude, tmp_path)
    runner.verify_by_task = {"t1": ["echo hi"]}
    rows = runner._verify_rows(tmp_path, "t1", "a patch", "some output")
    assert [row["command"] for row in rows] == ["echo hi", "restore scratch"]
    assert "could not be restored" in rows[1]["output"]


def test_a_threaded_transient_retry_verifies_only_the_attempt_that_stands(sequenced_claude, tmp_path, repo) -> None:
    script, set_sequence, _ = sequenced_claude
    set_sequence(SAFEGUARD, OK)
    marker = tmp_path / "ran"
    runner = ClaudeCodeRunner(PROFILE, claude_bin=str(script), cwd=tmp_path, repo_dir=repo)
    runner.verify_by_task = {"t1": [f"echo ran >> {marker}"]}
    out = runner.run(role="build", schema=SCHEMA, prompt="go", thread="T", task="t1")
    assert out["commands_run"][-1]["command"] == f"echo ran >> {marker}"
    assert marker.read_text(encoding="utf-8") == "ran\n"


def test_a_build_without_verify_commands_or_for_another_task_gets_no_rows(fake_claude, tmp_path, repo) -> None:
    runner = runner_for(fake_claude, tmp_path, repo_dir=repo)
    assert runner.verify_by_task == {}
    assert "commands_run" not in runner.run(role="build", schema=SCHEMA, prompt="go", task="t1")
    runner.verify_by_task = {"t2": ["echo other"]}
    assert "commands_run" not in runner.run(role="build", schema=SCHEMA, prompt="go", task="t1")


def test_a_reported_patch_that_does_not_apply_is_patch_outside_scratch(tmp_path, repo, conn) -> None:
    script = _reporting_claude(tmp_path, F_PATCH.replace("-one", "-absent"))
    runner = ClaudeCodeRunner(
        PROFILE, claude_bin=str(script), cwd=tmp_path, repo_dir=repo, runs_dir=tmp_path, run_id="r1", store=Store(conn)
    )
    with pytest.raises(RunnerError, match="patch_outside_scratch"):
        runner.run(role="build", schema=SCHEMA, prompt="go")
    assert [row["ok"] for row in _ledger_lines(conn, "r1")] == [False]


def test_apply_reported_patch_applies_a_valid_diff_and_refuses_a_bad_one(repo) -> None:
    assert apply_reported_patch(repo, "not a diff") is False
    assert (repo / "f.txt").read_text() == "one\n"
    assert apply_reported_patch(repo, F_PATCH) is True
    assert (repo / "f.txt").read_text() == "two\n"


def test_redirect_paths_moves_paths_under_the_repo_and_leaves_lookalikes() -> None:
    repo_dir, scratch = Path("/w/run/build"), Path("/tmp/scratch")
    assert redirect_paths("cd /w/run/build && sed x /w/run/build/a.py", repo_dir, scratch) == "cd /tmp/scratch && sed x /tmp/scratch/a.py"
    assert redirect_paths("at /w/run/build.", repo_dir, scratch) == "at /tmp/scratch."
    assert redirect_paths("/w/run/build2/a /x/w/run/build/a", repo_dir, scratch) == "/w/run/build2/a /x/w/run/build/a"
    assert redirect_paths("nothing here", repo_dir, scratch) == "nothing here"


def test_a_builders_argv_grants_the_scratch_and_not_the_repo(fake_claude, tmp_path, repo) -> None:
    runner = runner_for(fake_claude, tmp_path, repo_dir=repo)
    runner.tools["build"] = ["Read", "Write", "Edit", "Bash"]
    runner.run(role="build", schema=SCHEMA, prompt="go")
    argv = recorded(fake_claude)["argv"]
    added = [argv[i + 1] for i, a in enumerate(argv) if a == "--add-dir"]
    assert len(added) == 1 and "agent-graphs-build-" in added[0]
    assert str(repo.resolve()) not in argv[argv.index("--system-prompt") + 1]


def test_a_builders_prompt_has_the_repo_paths_rewritten_to_its_scratch(fake_claude, tmp_path, repo) -> None:
    runner = runner_for(fake_claude, tmp_path, repo_dir=repo)
    runner.run(role="build", schema=SCHEMA, prompt=f"edit {repo.resolve()}/f.txt then cd {repo.resolve()}")
    rec = recorded(fake_claude)
    scratch = next(rec["argv"][i + 1] for i, a in enumerate(rec["argv"]) if a == "--add-dir")
    assert rec["stdin"] == f"edit {scratch}/f.txt then cd {scratch}"


def test_a_reading_role_keeps_the_repo_and_its_prompt(fake_claude, tmp_path, repo) -> None:
    runner = runner_for(fake_claude, tmp_path, repo_dir=repo)
    runner.run(role="review_charter", schema=SCHEMA, prompt=f"read {repo.resolve()}/f.txt")
    rec = recorded(fake_claude)
    assert str(repo.resolve()) in rec["argv"] and rec["stdin"] == f"read {repo.resolve()}/f.txt"


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


def _bounds_file(tmp_path: Path, rows: list[dict]) -> Path:
    path = tmp_path / "bounds.json"
    path.write_text(json.dumps({"generated": "2026-09-16", "db": "stats.db", "rows": rows}), encoding="utf-8")
    return path


def test_a_bounds_row_with_enough_history_sets_the_ceiling_and_its_source(fake_claude, tmp_path) -> None:
    script, _, _ = fake_claude
    bounds = _bounds_file(tmp_path, [{"role": "build", "model": "sonnet", "n": 25, "strict": 0.1, "moderate": 0.2, "liberal": 0.6}])
    runner = ClaudeCodeRunner({**PROFILE, "role_budget_usd": {"build": 0.6}}, claude_bin=str(script), cwd=tmp_path)
    runner.cost_bounds_path = bounds
    runner.run(role="build", schema=SCHEMA, prompt="go")
    argv = recorded(fake_claude)["argv"]
    assert argv[argv.index("--max-budget-usd") + 1] == "0.2000"
    assert runner.calls[-1]["ceiling_usd"] == 0.2
    assert runner.calls[-1]["ceiling_source"] == "bounds:moderate"


def test_a_bounds_row_with_too_little_history_falls_back_to_the_profile(fake_claude, tmp_path) -> None:
    script, _, _ = fake_claude
    bounds = _bounds_file(tmp_path, [{"role": "build", "model": "sonnet", "n": 10, "strict": 0.1, "moderate": 0.2, "liberal": 0.6}])
    runner = ClaudeCodeRunner({**PROFILE, "role_budget_usd": {"build": 0.6}}, claude_bin=str(script), cwd=tmp_path)
    runner.cost_bounds_path = bounds
    runner.run(role="build", schema=SCHEMA, prompt="go")
    argv = recorded(fake_claude)["argv"]
    assert argv[argv.index("--max-budget-usd") + 1] == "0.6000"
    assert runner.calls[-1]["ceiling_usd"] == 0.6
    assert runner.calls[-1]["ceiling_source"] == "profile"


def test_a_bounds_path_that_does_not_resolve_falls_back_to_the_profile(fake_claude, tmp_path) -> None:
    script, _, _ = fake_claude
    runner = ClaudeCodeRunner({**PROFILE, "role_budget_usd": {"build": 0.6}}, claude_bin=str(script), cwd=tmp_path)
    runner.cost_bounds_path = tmp_path / "missing-bounds.json"
    runner.run(role="build", schema=SCHEMA, prompt="go")
    argv = recorded(fake_claude)["argv"]
    assert argv[argv.index("--max-budget-usd") + 1] == "0.6000"
    assert runner.calls[-1]["ceiling_usd"] == 0.6
    assert runner.calls[-1]["ceiling_source"] == "profile"


def test_no_bounds_file_behaves_exactly_as_today(fake_claude, tmp_path) -> None:
    script, _, _ = fake_claude
    runner = ClaudeCodeRunner({**PROFILE, "role_budget_usd": {"build": 0.6}}, claude_bin=str(script), cwd=tmp_path)
    runner.run(role="build", schema=SCHEMA, prompt="go")
    argv = recorded(fake_claude)["argv"]
    assert argv[argv.index("--max-budget-usd") + 1] == "0.6000"
    assert runner.calls[-1]["ceiling_usd"] == 0.6
    assert runner.calls[-1]["ceiling_source"] == "profile"


def test_a_strict_level_picks_the_strict_column(fake_claude, tmp_path) -> None:
    script, _, _ = fake_claude
    bounds = _bounds_file(tmp_path, [{"role": "build", "model": "sonnet", "n": 25, "strict": 0.1, "moderate": 0.2, "liberal": 0.6}])
    runner = ClaudeCodeRunner({**PROFILE, "role_budget_usd": {"build": 0.6}}, claude_bin=str(script), cwd=tmp_path)
    runner.cost_bounds_path = bounds
    runner.cost_level = "strict"
    runner.run(role="build", schema=SCHEMA, prompt="go")
    argv = recorded(fake_claude)["argv"]
    assert argv[argv.index("--max-budget-usd") + 1] == "0.1000"
    assert runner.calls[-1]["ceiling_source"] == "bounds:strict"


def test_a_node_cap_below_the_shape_ceiling_becomes_the_effective_limit(fake_claude, tmp_path) -> None:
    script, _, _ = fake_claude
    runner = ClaudeCodeRunner({**PROFILE, "budget_usd": {"standard": 2.0}}, claude_bin=str(script), cwd=tmp_path)
    runner.node_cap_usd = 0.5
    runner.run(role="plan", schema=SCHEMA, prompt="go")
    argv = recorded(fake_claude)["argv"]
    assert argv[argv.index("--max-budget-usd") + 1] == "0.5000"


def test_a_node_cap_above_the_shape_ceiling_leaves_the_ceiling_effective(fake_claude, tmp_path) -> None:
    script, _, _ = fake_claude
    runner = ClaudeCodeRunner({**PROFILE, "budget_usd": {"standard": 0.35}}, claude_bin=str(script), cwd=tmp_path)
    runner.node_cap_usd = 2.0
    runner.run(role="plan", schema=SCHEMA, prompt="go")
    argv = recorded(fake_claude)["argv"]
    assert argv[argv.index("--max-budget-usd") + 1] == "0.3500"


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
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "pytest -q"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "39 passed"}]}},
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
    assert trace.is_file() and len(trace.read_text().splitlines()) == 5
    assert runner.calls[-1]["trace"] == str(trace) and runner.calls[-1]["turns"] == 2
    assert runner.calls[-1]["commands_run"] == [{"command": "pytest -q", "output": "39 passed", "source": "trace"}]
    runner.run(role="build", schema=SCHEMA, prompt="again")
    assert (tmp_path / "trace" / "build-2.jsonl").is_file(), "one file per call, numbered per role"


# NOT a capture: no recorded stream exists in the repo and none could be made here. Keys follow the
# documented Claude Code system/init message. Replace with a real `--output-format stream-json` line.
INIT = {"type": "system", "subtype": "init", "claude_code_version": "2.0.31", "model": "claude-haiku-4-5-20251001"}
RESULT = {"type": "result", "subtype": "success", "is_error": False, "structured_output": {"ok": True}, "num_turns": 1}


def _streamed(tmp_path: Path, fake_claude, events: list[dict], **profile) -> ClaudeCodeRunner:
    script, _, _ = fake_claude
    (tmp_path / "output.json").write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    return ClaudeCodeRunner({**PROFILE, **profile}, claude_bin=str(script), cwd=tmp_path, trace_dir=tmp_path / "trace")


def test_init_facts_reads_version_and_model_and_gives_none_for_what_is_missing() -> None:
    assert _init_facts(INIT) == ("2.0.31", "claude-haiku-4-5-20251001")
    assert _init_facts({"model": "m"}) == (None, "m")
    assert _init_facts({"claude_code_version": "1"}) == ("1", None)
    assert _init_facts({"model": 3, "claude_code_version": ["x"]}) == (None, None)
    assert _init_facts(None) == (None, None)


def test_the_decision_carries_version_and_model_from_the_init_event(fake_claude, tmp_path) -> None:
    runner = _streamed(tmp_path, fake_claude, [INIT, RESULT])
    out = runner.run(role="build", tier="standard", schema=SCHEMA, prompt="go", thread="T-1", task="t9")
    d = out.decision
    assert (d.claude_code_version, d.model_id) == ("2.0.31", "claude-haiku-4-5-20251001")
    assert (d.role, d.requested_tier, d.chosen_tier, d.reason) == ("build", "standard", "reason", "caller")
    assert (d.ticket_key, d.outcome_key) == ("t9", ""), "the ticket is `task`, as in the ledger's task_id; a thread is not a ticket"
    assert dict(out) == {"ok": True}, "the decision is an attribute, not a key"


def test_parse_version_reads_numbers_and_pre_release_and_refuses_anything_else() -> None:
    assert [_parse_version(t) for t in ("2.1.3", "2.1.3 (Claude Code)", "2.1.0-beta.1")] == [((2, 1, 3), ""), ((2, 1, 3), ""), ((2, 1, 0), "beta.1")]
    assert [_parse_version(t) for t in (None, "", "v2.0.31", "latest", "2.1.3abc")] == [None] * 5


def test_floor_equal_below_above_and_numeric() -> None:
    assert [_version_violations(v, None, "2.9.0") for v in ("2.9.0", "2.8.9", "2.10.0", "2.9.0-rc.1")] == [
        (), ("version_below_floor",), (), ("version_below_floor",)
    ]


def test_pin_mismatch_pads_short_versions_and_counts_a_pre_release() -> None:
    assert [_version_violations(v, "2.1.0", None) for v in ("2.1.0", "2.1", "2.1.1", "2.1.0-beta")] == [
        (), (), ("version_pin_mismatch",), ("version_pin_mismatch",)
    ]


def test_a_missing_version_is_unknown_only_when_a_bound_is_set() -> None:
    assert [_version_violations(None, p, f) for p, f in (("2.0.31", None), (None, "2.0.31"), (None, None))] == [
        ("version_unknown",), ("version_unknown",), ()
    ]


def test_an_unparseable_bound_is_recorded_not_skipped() -> None:
    assert _version_violations("2.0.31", "v2.0.31", None) == ("version_bound_unparseable",)
    assert _version_violations("2.0.31", "2.0.30", "latest") == ("version_bound_unparseable", "version_pin_mismatch")
    assert _version_violations(None, None, "latest") == ("version_bound_unparseable", "version_unknown")


@pytest.mark.parametrize(
    ("bounds", "init", "reason"),
    [
        ({}, INIT, "caller"),
        ({"cli_version_floor": "2.1.0"}, INIT, "caller; version_below_floor"),
        ({"cli_version_pin": "2.0.30"}, INIT, "caller; version_pin_mismatch"),
        ({"cli_version_pin": "v2.0.31"}, INIT, "caller; version_bound_unparseable"),
        ({"cli_version_floor": "2.0.0"}, {"type": "system", "subtype": "init"}, "caller; version_unknown"),
    ],
)
def test_a_version_violation_rides_on_the_reason_and_the_call_still_finishes(fake_claude, tmp_path, bounds, init, reason) -> None:
    out = _streamed(tmp_path, fake_claude, [init, RESULT], **bounds).run(role="build", tier="standard", schema=SCHEMA, prompt="go")
    assert (dict(out), out.decision.reason) == ({"ok": True}, reason)


def test_a_tier_override_that_changes_the_tier_is_reason_override(fake_claude, tmp_path) -> None:
    runner = _streamed(tmp_path, fake_claude, [INIT, RESULT], tier_overrides={"build": "cheap"})
    d = runner.run(role="build", tier="standard", schema=SCHEMA, prompt="go").decision
    assert (d.requested_tier, d.chosen_tier, d.reason) == ("standard", "extract", "override")


def test_an_override_to_the_tier_already_asked_for_is_still_reason_override(fake_claude, tmp_path) -> None:
    runner = _streamed(tmp_path, fake_claude, [INIT, RESULT], tier_overrides={"build": "standard"})
    d = runner.run(role="build", tier="standard", schema=SCHEMA, prompt="go").decision
    assert (d.chosen_tier, d.reason) == ("reason", "override")


def test_an_init_event_lacking_the_fields_gives_none_version_and_the_resolved_model(fake_claude, tmp_path) -> None:
    runner = _streamed(tmp_path, fake_claude, [{"type": "system", "subtype": "init"}, RESULT])
    d = runner.run(role="build", tier="standard", schema=SCHEMA, prompt="go").decision
    assert d.claude_code_version is None and d.model_id == "sonnet"


def test_json_mode_has_no_init_event_and_still_yields_a_decision(fake_claude, tmp_path) -> None:
    d = runner_for(fake_claude, tmp_path).run(role="build", tier="deep", schema=SCHEMA, prompt="go").decision
    assert d.claude_code_version is None and d.model_id == "opus" and d.reason == "caller"


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


# ── derived evidence: files_touched, commands_run ────────────────────────────


def _tool_use(tool_id: str, command: str) -> dict:
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": tool_id, "name": "Bash", "input": {"command": command}}]}}


def _tool_result(tool_id: str, output: str) -> dict:
    return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": tool_id, "content": output}]}}


def test_trace_commands_returns_bash_commands_in_call_order() -> None:
    trace = [
        _tool_use("t1", "pytest -q"),
        _tool_result("t1", "39 passed"),
        _tool_use("t2", "ruff check ."),
        _tool_result("t2", "All checks passed!"),
    ]
    assert trace_commands(trace) == [
        {"command": "pytest -q", "output": "39 passed", "source": "trace"},
        {"command": "ruff check .", "output": "All checks passed!", "source": "trace"},
    ]


def test_trace_commands_with_no_bash_calls_returns_nothing() -> None:
    trace = [
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "/r/f.py"}}]}},
        {"type": "result", "subtype": "success"},
    ]
    assert trace_commands(trace) == []


def test_self_reported_commands_tags_the_models_own_list() -> None:
    assert self_reported_commands({"commands_run": ["pytest -q", "ruff check ."]}) == [
        {"command": "pytest -q", "source": "self_report"},
        {"command": "ruff check .", "source": "self_report"},
    ]


def test_self_reported_commands_with_none_reported_is_nothing() -> None:
    assert self_reported_commands({}) == []


def test_files_touched_from_patch_reads_the_plus_and_minus_headers() -> None:
    patch = (
        "diff --git a/runner/x.py b/runner/x.py\n"
        "--- a/runner/x.py\n"
        "+++ b/runner/x.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
        "diff --git a/tests/y.py b/tests/y.py\n"
        "--- a/tests/y.py\n"
        "+++ b/tests/y.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )
    assert files_touched_from_patch(patch) == ["runner/x.py", "tests/y.py"]


def test_files_touched_from_patch_with_no_patch_returns_nothing() -> None:
    assert files_touched_from_patch("") == []


def test_files_touched_from_patch_names_a_deleted_file_by_its_minus_header() -> None:
    patch = (
        "diff --git a/a.py b/a.py\n"
        "--- a/a.py\n"
        "+++ b/a.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
        "diff --git a/b.py b/b.py\n"
        "deleted file mode 100644\n"
        "--- a/b.py\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n"
        "-old\n"
    )
    assert files_touched_from_patch(patch) == ["a.py", "b.py"]


def test_files_touched_from_patch_strips_mnemonic_prefixes_too() -> None:
    """`git -c diff.mnemonicPrefix=true diff` headers read `c/... i/...`, not `a/... b/...`."""
    patch = "diff --git c/built.txt i/built.txt\n--- /dev/null\n+++ i/built.txt\n@@ -0,0 +1 @@\n+edited\n"
    assert files_touched_from_patch(patch) == ["built.txt"]


# ── the per-call ledger ──────────────────────────────────────────────────────


def _ledger_lines(conn, run_id: str) -> list[dict]:
    """The run's calls as the store holds them, shaped like the file ledger's rows were."""
    return [
        {
            **(r["detail_json"] or {}),
            "id": r["call_id"],
            "model": r["model_alias"],
            "ok": bool(r["ok"]),
            "role": r["role"],
            "cost_usd": r["cost_usd"],
            "decision": r["decision_json"],
        }
        for r in store_calls(conn, run_id)
    ]


def test_a_successful_call_records_one_row_and_writes_no_calls_file(fake_claude, tmp_path, conn) -> None:
    runner = runner_for(fake_claude, tmp_path, runs_dir=tmp_path, run_id="r1", store=Store(conn))
    runner.run(role="plan", schema=SCHEMA, prompt="go")
    rows = _ledger_lines(conn, "r1")
    assert len(rows) == 1
    assert rows[0]["ok"] is True
    assert rows[0]["role"] == runner.calls[-1]["role"] and rows[0]["cost_usd"] == runner.calls[-1]["cost_usd"]
    assert not list(tmp_path.glob("*.calls.jsonl"))


def test_a_runner_error_still_records_a_row_before_raising(fake_claude, tmp_path, conn) -> None:
    _, _, set_output = fake_claude
    set_output({"is_error": True, "result": "Not logged in · Please run /login"})
    runner = runner_for(fake_claude, tmp_path, runs_dir=tmp_path, run_id="r1", store=Store(conn))
    with pytest.raises(RunnerError, match="Not logged in"):
        runner.run(role="plan", schema=SCHEMA, prompt="go")
    assert [row["ok"] for row in _ledger_lines(conn, "r1")] == [False]


def test_a_non_object_answer_still_records_a_row_before_raising(fake_claude, tmp_path, conn) -> None:
    _, _, set_output = fake_claude
    set_output({"is_error": False, "structured_output": [1, 2, 3]})
    runner = runner_for(fake_claude, tmp_path, runs_dir=tmp_path, run_id="r1", store=Store(conn))
    with pytest.raises(RunnerError, match="expected an object"):
        runner.run(role="plan", schema=SCHEMA, prompt="go")
    assert [row["ok"] for row in _ledger_lines(conn, "r1")] == [False]


def test_prose_with_no_structured_output_still_records_a_row_before_raising(fake_claude, tmp_path, conn) -> None:
    _, _, set_output = fake_claude
    set_output({"is_error": False, "structured_output": None, "result": "I could not decide."})
    runner = runner_for(fake_claude, tmp_path, runs_dir=tmp_path, run_id="r1", store=Store(conn))
    with pytest.raises(RunnerError, match="not JSON"):
        runner.run(role="plan", schema=SCHEMA, prompt="go")
    assert [row["ok"] for row in _ledger_lines(conn, "r1")] == [False]


def test_two_calls_record_two_rows(fake_claude, tmp_path, conn) -> None:
    runner = runner_for(fake_claude, tmp_path, runs_dir=tmp_path, run_id="r1", store=Store(conn))
    runner.run(role="plan", schema=SCHEMA, prompt="go")
    runner.run(role="plan", schema=SCHEMA, prompt="again")
    assert len(_ledger_lines(conn, "r1")) == 2


def test_the_trace_file_exists_after_the_call_for_the_live_views(fake_claude, tmp_path, conn) -> None:
    """The CLI compacts trace files only at run end; until then `cox runs top` and `detail` read them."""
    runner = runner_for(fake_claude, tmp_path, runs_dir=tmp_path, run_id="r1", store=Store(conn), trace_dir=tmp_path / "trace")
    runner.run(role="plan", schema=SCHEMA, prompt="go")
    (row,) = _ledger_lines(conn, "r1")
    assert Path(row["trace"]).is_file() and Path(row["trace"]).parent == tmp_path / "trace"


def test_a_failing_store_write_logs_an_error_naming_the_call(fake_claude, tmp_path, caplog) -> None:
    class Boom:
        def record_call(self, *args, **kwargs):
            raise RuntimeError("disk full")

    runner = runner_for(fake_claude, tmp_path, runs_dir=tmp_path, run_id="r1", store=Boom())
    with caplog.at_level("ERROR"):
        runner.run(role="plan", schema=SCHEMA, prompt="go")
    (record,) = [r for r in caplog.records if "store write failed" in r.getMessage()]
    assert record.levelname == "ERROR" and runner.calls[-1]["id"] in record.getMessage()


def test_without_runs_dir_or_run_id_nothing_is_written(fake_claude, tmp_path) -> None:
    runner = runner_for(fake_claude, tmp_path)
    runner.run(role="plan", schema=SCHEMA, prompt="go")
    assert not list(tmp_path.glob("*.calls.jsonl"))


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


def test_the_builder_is_told_to_run_every_check_and_may_run_the_lint_command(fake_claude, tmp_path, repo) -> None:
    """A lint error the builder never ran fails after review and costs a rerun (B023, 2026-09-23)."""
    runner = runner_for(fake_claude, tmp_path, repo_dir=repo)
    runner.tools["build"] = ["Read", "Write", "Edit", "Bash"]
    runner.check_commands = ["pytest -q", "ruff check ."]
    runner.run(role="build", schema=SCHEMA, prompt="go")
    argv = recorded(fake_claude)["argv"]
    system = argv[argv.index("--system-prompt") + 1]
    assert "Run every one of them before you produce the diff, a lint command as much as the tests" in system
    allowed = argv[argv.index("--allowedTools") + 1 : argv.index("--tools")]
    assert "Bash(ruff:*)" in allowed, "the sentence is worthless unless the sandbox lets the lint command run"
    runner.check_commands = []
    runner.run(role="build", schema=SCHEMA, prompt="go")
    argv = recorded(fake_claude)["argv"]
    assert "a lint command as much as the tests" not in argv[argv.index("--system-prompt") + 1], "no checks, nothing to run"


def test_bash_is_pre_approved_for_the_checks_and_git_and_nothing_else(fake_claude, tmp_path, repo) -> None:
    """acceptEdits never covered Bash: seven epics of builds never ran a test."""
    runner = runner_for(fake_claude, tmp_path, repo_dir=repo)
    runner.tools["build"] = ["Read", "Write", "Edit", "Bash"]
    runner.check_commands = ["pytest -q", "ruff check ."]
    runner.run(role="build", schema=SCHEMA, prompt="go")
    argv = recorded(fake_claude)["argv"]
    i = argv.index("--allowedTools")
    allowed = argv[i + 1 : argv.index("--tools")]
    assert allowed == [
        "Bash(pytest:*)",
        "Bash(ruff:*)",
        "Bash(python -m pytest:*)",
        "Bash(python3 -m pytest:*)",
        "Bash(python -m ruff:*)",
        "Bash(python3 -m ruff:*)",
        "Bash(git status:*)",
        "Bash(git diff:*)",
        "Bash(git add:*)",
    ]
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
STRUCTURED_OUTPUT_ERROR = {
    "type": "result",
    "is_error": True,
    "subtype": "error_max_structured_output_retries",
    "result": "Failed to provide valid structured output after 5 attempts: the response could not be parsed as JSON",
    "num_turns": 5,
}


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
        "if pathlib.Path('.git').exists():\n"
        "    pathlib.Path('built.txt').write_text('edited\\n')\n"
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


def test_is_safeguard_refusal_matches_the_literal_refusal_text_but_not_an_ordinary_error() -> None:
    assert is_safeguard_refusal(SAFEGUARD) is True
    assert is_safeguard_refusal(REFUSED) is False


def test_alt_model_for_returns_the_next_binding_or_falls_back_to_the_same_model() -> None:
    tiers = {"standard": ["sonnet", "opus"], "cheap": "haiku"}
    assert _alt_model_for(tiers, "standard", "sonnet") == "opus"
    assert _alt_model_for(tiers, "standard", "opus") == "sonnet"
    assert _alt_model_for(tiers, "cheap", "haiku") == "haiku"


def test_a_safeguard_refusal_retries_once_on_the_alternate_model(sequenced_claude, tmp_path, conn) -> None:
    script, set_sequence, _ = sequenced_claude
    set_sequence(SAFEGUARD, OK)
    profile = {**PROFILE, "tiers": {**PROFILE["tiers"], "standard": ["sonnet", "opus"]}}
    runner = ClaudeCodeRunner(profile, claude_bin=str(script), cwd=tmp_path, runs_dir=tmp_path, run_id="r1", store=Store(conn))

    assert dict(runner.run(role="arbitrate", schema=SCHEMA, prompt="decide")) == {"ok": True}
    rows = _ledger_lines(conn, "r1")
    assert len(rows) == 2
    assert rows[1]["retry_of"] == rows[0]["id"]
    assert rows[1]["reason"] == "safeguard_refusal"
    assert rows[1]["model"] == "opus"


def test_a_structured_output_error_retries_once_on_the_alternate_model(sequenced_claude, tmp_path, conn) -> None:
    script, set_sequence, _ = sequenced_claude
    set_sequence(STRUCTURED_OUTPUT_ERROR, OK)
    profile = {**PROFILE, "tiers": {**PROFILE["tiers"], "standard": ["sonnet", "opus"]}}
    runner = ClaudeCodeRunner(profile, claude_bin=str(script), cwd=tmp_path, runs_dir=tmp_path, run_id="r1", store=Store(conn))

    assert dict(runner.run(role="arbitrate", schema=SCHEMA, prompt="decide")) == {"ok": True}
    rows = _ledger_lines(conn, "r1")
    assert len(rows) == 2
    assert rows[1]["retry_of"] == rows[0]["id"]
    assert rows[1]["reason"] == "structured_output"
    assert rows[1]["model"] == "opus"


def test_a_safeguard_refusal_that_recurs_on_the_alternate_model_propagates_the_original_error(
    sequenced_claude, tmp_path, conn
) -> None:
    script, set_sequence, _ = sequenced_claude
    set_sequence(SAFEGUARD, SAFEGUARD)
    profile = {**PROFILE, "tiers": {**PROFILE["tiers"], "standard": ["sonnet", "opus"]}}
    runner = ClaudeCodeRunner(profile, claude_bin=str(script), cwd=tmp_path, runs_dir=tmp_path, run_id="r1", store=Store(conn))

    with pytest.raises(RunnerError, match="safeguards flagged"):
        runner.run(role="arbitrate", schema=SCHEMA, prompt="decide")
    rows = _ledger_lines(conn, "r1")
    assert len(rows) == 2
    assert rows[1]["retry_of"] == rows[0]["id"]
    assert rows[1]["model"] == "opus"


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


def test_a_budget_stop_governed_by_the_node_cap_is_error_spend_cap_with_both_numbers(sequenced_claude, tmp_path) -> None:
    script, set_sequence, _ = sequenced_claude
    set_sequence(REFUSED)
    runner = ClaudeCodeRunner({**PROFILE, "budget_usd": {"standard": 2.0}}, claude_bin=str(script), cwd=tmp_path)
    runner.node_cap_usd = 0.5

    with pytest.raises(BudgetStop) as exc_info:
        runner.run(role="build", schema=SCHEMA, prompt="build it")
    assert "error_spend_cap" in exc_info.value.detail
    assert "0.5000" in exc_info.value.detail and "2.0000" in exc_info.value.detail


def test_a_budget_stop_with_the_cap_looser_than_the_ceiling_stays_error_max_budget_usd(sequenced_claude, tmp_path) -> None:
    script, set_sequence, _ = sequenced_claude
    set_sequence(REFUSED)
    runner = ClaudeCodeRunner({**PROFILE, "budget_usd": {"standard": 0.5}}, claude_bin=str(script), cwd=tmp_path)
    runner.node_cap_usd = 2.0

    with pytest.raises(BudgetStop) as exc_info:
        runner.run(role="build", schema=SCHEMA, prompt="build it")
    assert "error_max_budget_usd" in exc_info.value.detail
    assert "error_spend_cap" not in exc_info.value.detail


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


# ── the ledger sees every billed attempt, not just the one that returns ─────


def test_a_transient_retry_ledgers_the_failed_attempt_before_the_success(sequenced_claude, tmp_path, conn) -> None:
    script, set_sequence, _ = sequenced_claude
    set_sequence(SAFEGUARD, OK)
    runner = ClaudeCodeRunner(PROFILE, claude_bin=str(script), cwd=tmp_path, runs_dir=tmp_path, run_id="r1", store=Store(conn))

    assert dict(runner.run(role="arbitrate", schema=SCHEMA, prompt="decide")) == {"ok": True}
    rows = _ledger_lines(conn, "r1")
    assert len(rows) == 2, "the retried attempt was billed too and must not vanish from the ledger"
    assert [row["ok"] for row in rows] == [False, True]


def test_a_transient_retry_with_tracing_ledgers_the_renamed_trace_not_the_reused_one(
    sequenced_claude, tmp_path, conn
) -> None:
    """The retry reuses the failed attempt's filename, so its ledger row must not still point there."""
    script, set_sequence, _ = sequenced_claude
    set_sequence(SAFEGUARD, OK)
    runner = ClaudeCodeRunner(
        PROFILE, claude_bin=str(script), cwd=tmp_path, runs_dir=tmp_path, run_id="r1", store=Store(conn),
        trace_dir=tmp_path / "trace",
    )

    runner.run(role="arbitrate", schema=SCHEMA, prompt="decide")
    rows = _ledger_lines(conn, "r1")
    assert len(rows) == 2
    assert rows[0]["trace"] != rows[1]["trace"], "one path must not be claimed by two rows"
    assert rows[0]["trace"].endswith("arbitrate-1.error.jsonl")
    assert rows[1]["trace"].endswith("arbitrate-1.jsonl")


def test_a_budget_stop_still_ledgers_the_attempt_it_spent(sequenced_claude, tmp_path, conn) -> None:
    script, set_sequence, _ = sequenced_claude
    set_sequence(REFUSED)
    runner = ClaudeCodeRunner(PROFILE, claude_bin=str(script), cwd=tmp_path, runs_dir=tmp_path, run_id="r1", store=Store(conn))

    with pytest.raises(BudgetStop):
        runner.run(role="build", schema=SCHEMA, prompt="build it")
    assert [row["ok"] for row in _ledger_lines(conn, "r1")] == [False]


def test_a_patch_empty_refusal_is_not_ledgered_as_a_success(tmp_path, repo, conn) -> None:
    output = tmp_path / "output.json"
    output.write_text(
        json.dumps({"is_error": False, "total_cost_usd": 0.01, "num_turns": 1, "structured_output": {"patch": ""}}),
        encoding="utf-8",
    )
    script = tmp_path / "claude"
    script.write_text(f"#!/bin/sh\ncat {output}\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    runner = ClaudeCodeRunner(
        PROFILE, claude_bin=str(script), cwd=tmp_path, repo_dir=repo, runs_dir=tmp_path, run_id="r1", store=Store(conn)
    )
    with pytest.raises(RunnerError, match="patch_empty"):
        runner.run(role="build", schema=SCHEMA, prompt="go")
    assert [row["ok"] for row in _ledger_lines(conn, "r1")] == [False]


def test_a_patch_empty_retry_does_not_overwrite_the_failed_attempts_trace(tmp_path, repo) -> None:
    """The failed attempt must still occupy a slot in `self.calls`, or `_payload` numbers the retry's
    trace file the same as the one it just lost — the exact loss this initiative exists to close."""
    failed = tmp_path / "failed.json"
    failed.write_text(
        json.dumps({"type": "result", "is_error": False, "total_cost_usd": 0.01, "num_turns": 1, "structured_output": {"patch": ""}}) + "\n",
        encoding="utf-8",
    )
    ok = tmp_path / "ok.json"
    ok.write_text(
        json.dumps({"type": "result", "is_error": False, "total_cost_usd": 0.01, "num_turns": 1, "structured_output": {"summary": "done"}}) + "\n",
        encoding="utf-8",
    )
    marker = tmp_path / "attempted"
    script = tmp_path / "claude"
    script.write_text(
        f"#!/bin/sh\nif [ -f {marker} ]; then echo edited > built.txt; cat {ok}; else touch {marker}; cat {failed}; fi\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    runner = ClaudeCodeRunner(PROFILE, claude_bin=str(script), cwd=tmp_path, repo_dir=repo, trace_dir=tmp_path / "trace")
    with pytest.raises(RunnerError, match="patch_empty"):
        runner.run(role="build", schema=SCHEMA, prompt="go")
    runner.run(role="build", schema=SCHEMA, prompt="go")
    trace_dir = tmp_path / "trace"
    failed_trace = json.loads((trace_dir / "build-1.jsonl").read_text().splitlines()[-1])
    ok_trace = json.loads((trace_dir / "build-2.jsonl").read_text().splitlines()[-1])
    assert failed_trace["structured_output"] == {"patch": ""}
    assert ok_trace["structured_output"] == {"summary": "done"}


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

    assert enforced == [
        "Bash(uv:*)",
        "Bash(ruff:*)",
        "Bash(python -m ruff:*)",
        "Bash(python3 -m ruff:*)",
        "Bash(git status:*)",
        "Bash(git diff:*)",
        "Bash(git add:*)",
    ]
    for name in enforced:
        assert f"`{name[len('Bash('):-len(':*)')]}`" in system


def test_python_tool_checks_also_permit_the_python_m_form(fake_claude, tmp_path, repo) -> None:
    """Builders run `python -m pytest`; the first-word entry alone denied it every build."""
    runner = runner_for(fake_claude, tmp_path, repo_dir=repo)
    runner.check_commands = ["ruff check .", "pytest -q"]
    allowed = runner._allowed_bash()
    for name in ("Bash(python -m pytest:*)", "Bash(python -m ruff:*)", "Bash(python3 -m pytest:*)", "Bash(python3 -m ruff:*)"):
        assert name in allowed
    runner.check_commands = ["uv run pytest"]
    assert not any("-m" in name for name in runner._allowed_bash()), "only Python-tool checks gain the -m form"


def test_the_builder_is_told_each_check_verbatim_and_that_compound_commands_are_denied(fake_claude, tmp_path, repo) -> None:
    _, record, _ = fake_claude
    runner = runner_for(fake_claude, tmp_path, repo_dir=repo)
    runner.tools["build"] = ["Read", "Write", "Edit", "Bash"]
    runner.check_commands = ["ruff check .", "pytest -q"]
    runner.run(role="build", schema=SCHEMA, prompt="build it")
    system = _system_prompt(record)
    for cmd in runner.check_commands:
        assert f"`{cmd}`" in system
    assert "`python -m pytest -q`" in system
    assert "no `cd ... &&` prefix is needed" in system
    assert "denied as a whole" in system


# ── tier resolution: override, caller, profile default, floor ────────────────


def _tier_run(fake_claude, tmp_path, profile=None, **call):
    script, _, _ = fake_claude
    runner = ClaudeCodeRunner({**PROFILE, **(profile or {})}, claude_bin=str(script), cwd=tmp_path)
    decision = runner.run(role="plan", schema=SCHEMA, prompt="go", **call).decision
    argv = recorded(fake_claude)["argv"]
    return decision, argv[argv.index("--model") + 1], argv[argv.index("--effort") + 1]


def test_a_profile_default_beats_default_tier(fake_claude, tmp_path) -> None:
    d, model, effort = _tier_run(fake_claude, tmp_path, {"defaults": {"plan": "deep"}})
    assert (d.chosen_tier, d.reason, model, effort) == ("judge", "profile_default", "opus", "xhigh")


def test_a_tier_override_beats_a_profile_default(fake_claude, tmp_path) -> None:
    d, model, _ = _tier_run(fake_claude, tmp_path, {"defaults": {"plan": "deep"}, "tier_overrides": {"plan": "cheap"}})
    assert (d.chosen_tier, d.reason, model) == ("extract", "override", "haiku")


def test_no_tier_and_no_default_lands_on_the_floor(fake_claude, tmp_path) -> None:
    d, model, _ = _tier_run(fake_claude, tmp_path)
    assert (d.requested_tier, d.chosen_tier, d.reason, model) == ("standard", "reason", "floor", "sonnet")


def test_a_tier_the_caller_named_beats_a_profile_default(fake_claude, tmp_path) -> None:
    d, model, _ = _tier_run(fake_claude, tmp_path, {"defaults": {"plan": "cheap"}}, tier="deep")
    assert (d.chosen_tier, d.reason, model) == ("judge", "caller", "opus")


def test_an_explicit_budget_is_recorded_as_given_and_effort_comes_from_the_tier(fake_claude, tmp_path) -> None:
    d, _, _ = _tier_run(fake_claude, tmp_path, tier="deep", budget_usd=0.5)
    assert (d.budget_usd, d.effort, d.clipped_by) == (0.5, "xhigh", None)


def test_a_profile_default_naming_an_unbound_tier_is_named(fake_claude, tmp_path) -> None:
    with pytest.raises(RunnerError, match="no model for tier 'huge'"):
        _tier_run(fake_claude, tmp_path, {"defaults": {"plan": "huge"}})


BY_TIER = {"scope_epic": "cheap", "build": "standard", "arbitrate": "deep"}
BY_CLASS = {"scope_epic": "extract", "build": "reason", "arbitrate": "judge"}
BOUND = {"classes": {"extract": "haiku", "reason": "sonnet", "judge": "opus"}}


def _role_run(fake_claude, tmp_path, role, profile):
    script, _, _ = fake_claude
    runner = ClaudeCodeRunner({**PROFILE, **BOUND, **profile}, claude_bin=str(script), cwd=tmp_path)
    decision = runner.run(role=role, schema=SCHEMA, prompt="go").decision
    argv = recorded(fake_claude)["argv"]
    return decision.chosen_tier, decision.reason, argv[argv.index("--model") + 1], argv[argv.index("--effort") + 1]


@pytest.mark.parametrize("role", sorted(BY_TIER))
@pytest.mark.parametrize("key", ["defaults", "tier_overrides"])
def test_a_class_named_profile_resolves_like_the_tier_named_one(fake_claude, tmp_path, role, key) -> None:
    by_tier = _role_run(fake_claude, tmp_path, role, {key: BY_TIER})
    by_class = _role_run(fake_claude, tmp_path, role, {key: BY_CLASS})
    assert by_class == by_tier


def test_a_caller_may_name_a_class(fake_claude, tmp_path) -> None:
    d, model, _ = _tier_run(fake_claude, tmp_path, BOUND, tier="judge")
    assert (d.chosen_tier, d.reason, model) == ("judge", "caller", "opus")


def test_a_garbage_name_still_raises_and_names_both_vocabularies(fake_claude, tmp_path) -> None:
    with pytest.raises(RunnerError, match="cheap, standard, deep or extract, reason, judge, frontier"):
        _tier_run(fake_claude, tmp_path, {"tiers": {"cheap": "h", "standard": "s", "deep": "d", "bogus": "b"}, "defaults": {"plan": "bogus"}})


# ── the profile `classes` map ────────────────────────────────────────────────

CLASSES_PROFILE = {"classes": {"extract": ["haiku", "haiku-2"], "reason": "sonnet", "judge": ["opus", "opus-2"]}}


def test_a_class_resolves_to_the_first_classes_entry(fake_claude, tmp_path) -> None:
    d, model, _ = _tier_run(fake_claude, tmp_path, CLASSES_PROFILE, tier="deep")
    assert (d.chosen_tier, model) == ("judge", "opus")


def test_a_profile_without_classes_still_uses_the_tiers_map(fake_claude, tmp_path) -> None:
    d, model, _ = _tier_run(fake_claude, tmp_path, {"tiers": {"cheap": "h", "standard": "s", "deep": "d"}}, tier="deep")
    assert (d.chosen_tier, model) == ("judge", "d")


def test_a_class_the_profile_does_not_bind_falls_to_the_floor_model(fake_claude, tmp_path) -> None:
    _, model, _ = _tier_run(fake_claude, tmp_path, {"classes": {"extract": "haiku", "reason": "sonnet"}}, tier="deep")
    assert model == "haiku"


def test_a_class_keyed_effort_beats_the_legacy_tier_name(fake_claude, tmp_path) -> None:
    _, _, effort = _tier_run(fake_claude, tmp_path, {**CLASSES_PROFILE, "effort": {"judge": "max"}}, tier="deep")
    assert effort == "max"


def test_effort_without_a_class_key_reads_the_legacy_tier_name(fake_claude, tmp_path) -> None:
    _, _, effort = _tier_run(fake_claude, tmp_path, CLASSES_PROFILE, tier="deep")
    assert effort == "xhigh"


def test_a_safeguard_refusal_retries_on_the_next_classes_entry(sequenced_claude, tmp_path, conn) -> None:
    script, set_sequence, _ = sequenced_claude
    set_sequence(SAFEGUARD, OK)
    runner = ClaudeCodeRunner(
        {**PROFILE, **CLASSES_PROFILE}, claude_bin=str(script), cwd=tmp_path, runs_dir=tmp_path, run_id="r1", store=Store(conn)
    )
    runner.run(role="arbitrate", schema=SCHEMA, prompt="decide", tier="deep")
    assert [row["model"] for row in _ledger_lines(conn, "r1")] == ["opus", "opus-2"]


# ── a supplied shadow router decision is recorded, never obeyed ─────────────


ROUTER_FIELDS = ("router_tier", "router_reason", "router_model", "router_effort", "router_budget_usd", "router_clipped_by")


def _shadow_decision() -> RouterDecision:
    return RouterDecision(
        chosen_class="cheap",
        model="haiku-elsewhere",
        effort="low",
        budget_usd=0.25,
        reasons=("small diff", "no risky surface"),
        clipped_by=("chair_budget",),
    )


def _router_run(fake_claude, tmp_path, mode, decision):
    script, _, _ = fake_claude
    profile = PROFILE if mode is None else {**PROFILE, "router": mode}
    runner = ClaudeCodeRunner(profile, claude_bin=str(script), cwd=tmp_path)
    return runner.run(role="plan", tier="deep", schema=SCHEMA, prompt="go", router_decision=decision).decision


def test_shadow_fills_the_router_fields_and_leaves_the_run_class_and_model_alone(fake_claude, tmp_path) -> None:
    decision = _router_run(fake_claude, tmp_path, "shadow", _shadow_decision())
    assert decision.router_tier == "cheap"
    assert decision.router_reason == "small diff; no risky surface"
    assert decision.router_model == "haiku-elsewhere"
    assert decision.router_effort == "low"
    assert decision.router_budget_usd == 0.25
    assert decision.router_clipped_by == ("chair_budget",)
    assert decision.chosen_tier == "judge"
    assert decision.model_id == "opus"
    argv = recorded(fake_claude)["argv"]
    assert argv[argv.index("--model") + 1] == "opus"


@pytest.mark.parametrize("mode", [None, "off"])
def test_off_leaves_the_router_fields_none(fake_claude, tmp_path, mode) -> None:
    decision = _router_run(fake_claude, tmp_path, mode, _shadow_decision())
    assert [getattr(decision, f) for f in ROUTER_FIELDS] == [None] * len(ROUTER_FIELDS)


def test_on_warns_once_per_runner_and_acts_as_shadow(fake_claude, tmp_path, caplog) -> None:
    shadow = _router_run(fake_claude, tmp_path, "shadow", _shadow_decision())
    script, _, _ = fake_claude
    with caplog.at_level("WARNING", logger="runner.claude_code_runner"):
        runner = ClaudeCodeRunner({**PROFILE, "router": "on"}, claude_bin=str(script), cwd=tmp_path)
        decisions = [
            runner.run(role="plan", tier="deep", schema=SCHEMA, prompt="go", router_decision=_shadow_decision()).decision
            for _ in range(2)
        ]
    assert [r.getMessage() for r in caplog.records if r.levelname == "WARNING"] == [
        "profile router 'on' is not implemented; acting as 'shadow'"
    ]
    for decision in decisions:
        assert [getattr(decision, f) for f in ROUTER_FIELDS] == [getattr(shadow, f) for f in ROUTER_FIELDS]
        assert (decision.chosen_tier, decision.model_id) == ("judge", "opus")


@pytest.mark.parametrize(("text", "mode"), [("router: on", "on"), ("router: off", "off"), ("router: shadow", "shadow"), ("{}", "off")])
def test_a_yaml_profile_router_value_reaches_its_mode(tmp_path, text, mode) -> None:
    profile = {**PROFILE, **yaml.safe_load(text)}
    assert ClaudeCodeRunner(profile, claude_bin="claude", cwd=tmp_path).router_mode == mode


def test_an_unrecognised_router_value_warns_and_acts_as_off(tmp_path, caplog) -> None:
    with caplog.at_level("WARNING", logger="runner.claude_code_runner"):
        runner = ClaudeCodeRunner({**PROFILE, "router": "shadw"}, claude_bin="claude", cwd=tmp_path)
    assert runner.router_mode == "off"
    assert [r.getMessage() for r in caplog.records] == ["profile router 'shadw' is not off, shadow or on; acting as 'off'"]


def test_a_call_with_no_decision_leaves_the_router_fields_none(fake_claude, tmp_path) -> None:
    decision = _router_run(fake_claude, tmp_path, "shadow", None)
    assert [getattr(decision, f) for f in ROUTER_FIELDS] == [None] * len(ROUTER_FIELDS)
