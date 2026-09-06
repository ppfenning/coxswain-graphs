"""The check arm: run the project's commands, in the state they were applied to.

`_change_facts` in `graphs/delivery/lifecycle_propose.py` counts a diff's shape
from the patch instead of asking the build node to self-report it. These tests
hold `harness/checks.py` and `harness/worktree.py`'s `create_worktree` to the
same discipline: pass/fail counts come from parsing real subprocess output, a
check missing `name` or `cmd` is refused rather than quietly dropped, a check
that never finishes is a failure and not an absence, and — the one that would
be silently wrong if the wiring were off by a step — a check run after
`apply_patch` actually sees the patched file, not the pre-patch one.

Offline throughout: real `git` and `sys.executable`, no network.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

from harness.checks import (
    all_passed,
    check_outcome,
    checks_evidence,
    is_harness_fault,
    quarantine_reason,
    repo_checks,
    run_checks,
)
from harness.worktree import apply_patch, create_worktree


def _run(cmd: list[str], cwd) -> None:
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    assert result.returncode == 0, f"{cmd} failed: {result.stderr}"


def _init_repo(path, *, filename="a.txt", content="one\n"):
    """A tiny real git repo: init, local identity, one commit."""
    path.mkdir(parents=True, exist_ok=True)
    _run(["git", "init", "-q"], cwd=path)
    _run(["git", "config", "user.email", "test@example.com"], cwd=path)
    _run(["git", "config", "user.name", "Test"], cwd=path)
    (path / filename).write_text(content, encoding="utf-8")
    _run(["git", "add", filename], cwd=path)
    _run(["git", "commit", "-q", "-m", "initial"], cwd=path)
    return path


def _head(path) -> str:
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True)
    return result.stdout.strip()


def _py(code: str) -> str:
    """A shell-safe `sys.executable -c <code>` invocation for run_checks.

    Wrapped in literal double quotes rather than `repr()`'d: `repr` turns a
    real newline into the two characters `\\` and `n`, which survives a shell
    round-trip as those two characters rather than as a newline — the script
    it hands to `python -c` is not the script that was written. Every check
    command here sticks to single quotes internally so the double-quote
    wrapper never has to escape anything.
    """
    return f'{sys.executable} -c "{textwrap.dedent(code).strip()}"'


# ---------------------------------------------------------------------------
# run_checks
# ---------------------------------------------------------------------------


def test_run_checks_passing_cmd_reports_counts_and_passed(tmp_path) -> None:
    checks = [{"name": "tests", "cmd": _py("print('3 passed')")}]
    [result] = run_checks(tmp_path, checks)
    assert result["passed"] is True
    assert result["exit_code"] == 0
    assert result["counts"] == {"passed": 3}
    assert result["name"] == "tests"
    assert result["outcome"] == "passed"


def test_run_checks_failing_cmd_reports_both_counts_and_failure(tmp_path) -> None:
    """Mixed output ('1 failed, 2 passed') parses every token, not just the first."""
    checks = [
        {
            "name": "tests",
            "cmd": _py("import sys; print('1 failed, 2 passed'); sys.exit(1)"),
        }
    ]
    [result] = run_checks(tmp_path, checks)
    assert result["passed"] is False
    assert result["exit_code"] == 1
    assert result["counts"] == {"failed": 1, "passed": 2}
    assert result["outcome"] == "failed"


def test_run_checks_unrunnable_cmd_is_a_harness_fault_not_a_failure(tmp_path) -> None:
    missing = tmp_path / "no-such-worktree"
    checks = [{"name": "ghost", "cmd": "true"}]
    [result] = run_checks(missing, checks)
    assert result["outcome"] == check_outcome(result["exit_code"], result["error"])
    assert result["outcome"] == "unrunnable"
    assert result["exit_code"] is None
    assert "no-such-worktree" in result["error"]


def test_run_checks_missing_binary_is_unrunnable_naming_the_command(tmp_path) -> None:
    checks = [{"name": "tests", "cmd": "no-such-binary-xyz -q"}]
    [result] = run_checks(tmp_path, checks)
    assert result["outcome"] == "unrunnable"
    assert "no-such-binary-xyz -q" in result["error"]


def test_missing_binary_evidence_carries_the_command_and_error(tmp_path) -> None:
    checks = [{"name": "tests", "cmd": "no-such-binary-xyz -q"}]
    results = run_checks(tmp_path, checks)
    [row] = checks_evidence(results)
    assert "no-such-binary-xyz -q" in row["output"]
    assert "command not found" in row["output"]


def test_missing_binary_evidence_also_carries_the_shells_own_words(tmp_path) -> None:
    """The synthesized error names the command; the shell's own text must still survive beside it."""
    checks = [{"name": "tests", "cmd": "no-such-binary-xyz -q"}]
    results = run_checks(tmp_path, checks)
    assert results[0]["output_tail"].strip()
    [row] = checks_evidence(results)
    assert results[0]["output_tail"].strip() in row["output"]


def test_run_checks_no_counts_in_output_is_empty_not_invented(tmp_path) -> None:
    """A check whose output names no pass/fail tokens gets an EMPTY counts dict,
    never a guessed one — the whole point of counting from reality."""
    checks = [{"name": "lint", "cmd": _py("print('no complaints here')")}]
    [result] = run_checks(tmp_path, checks)
    assert result["passed"] is True
    assert result["counts"] == {}


def test_run_checks_missing_name_is_refused(tmp_path) -> None:
    with pytest.raises(ValueError):
        run_checks(tmp_path, [{"cmd": "true"}])


def test_run_checks_missing_cmd_is_refused(tmp_path) -> None:
    with pytest.raises(ValueError):
        run_checks(tmp_path, [{"name": "tests"}])


def test_run_checks_timeout_is_a_failure_not_an_absence(tmp_path) -> None:
    """A check that never finishes is not 'still pending' — it failed, with no
    exit code, and the output tail says so."""
    checks = [{"name": "hangs", "cmd": _py("import time; time.sleep(5)")}]
    [result] = run_checks(tmp_path, checks, timeout=0.3)
    assert result["passed"] is False
    assert result["exit_code"] is None
    assert "timed out" in result["output_tail"]


def test_run_checks_output_tail_is_bounded(tmp_path) -> None:
    checks = [{"name": "noisy", "cmd": _py("print('x' * 5000)")}]
    [result] = run_checks(tmp_path, checks)
    assert len(result["output_tail"]) <= 2000


# ---------------------------------------------------------------------------
# check_outcome
# ---------------------------------------------------------------------------


def test_check_outcome_is_passed_on_a_zero_return_with_no_error() -> None:
    assert check_outcome(0, None) == "passed"


def test_check_outcome_is_failed_on_a_nonzero_return_with_no_error() -> None:
    assert check_outcome(1, None) == "failed"


def test_check_outcome_is_unrunnable_when_an_error_is_present() -> None:
    assert check_outcome(None, "No such file or directory") == "unrunnable"


# ---------------------------------------------------------------------------
# checks_evidence
# ---------------------------------------------------------------------------


def test_checks_evidence_verdict_first_with_counts() -> None:
    results = [{"name": "tests", "cmd": "pytest -q", "passed": True, "exit_code": 0, "counts": {"passed": 12}}]
    [row] = checks_evidence(results)
    assert row["check"] == "checks:tests"
    assert row["output"].startswith("pass —")
    assert "12 passed" in row["output"]
    assert "exit 0" in row["output"]


def test_checks_evidence_failure_is_all_caps_fail_with_exit_code() -> None:
    results = [{"name": "tests", "cmd": "pytest -q", "passed": False, "exit_code": 1, "counts": {"failed": 2, "passed": 10}}]
    [row] = checks_evidence(results)
    assert row["output"].startswith("FAIL —")
    assert "2 failed" in row["output"]
    assert "10 passed" in row["output"]
    assert "exit 1" in row["output"]


def test_checks_evidence_exit_code_present_even_with_no_counts() -> None:
    results = [{"name": "lint", "cmd": "lint", "passed": True, "exit_code": 0, "counts": {}}]
    [row] = checks_evidence(results)
    assert "exit 0" in row["output"]


def test_checks_evidence_failure_carries_the_command_and_output_tail() -> None:
    results = [
        {
            "name": "tests",
            "cmd": "pytest -q",
            "passed": False,
            "outcome": "failed",
            "exit_code": 1,
            "counts": {"failed": 1},
            "output_tail": "AssertionError: boom",
        }
    ]
    [row] = checks_evidence(results)
    assert "cmd: pytest -q" in row["output"]
    assert "AssertionError: boom" in row["output"]


def test_checks_evidence_truncates_a_long_tail_with_a_marker() -> None:
    lines = [f"line {i}" for i in range(25)]
    results = [
        {
            "name": "tests",
            "cmd": "pytest -q",
            "passed": False,
            "outcome": "failed",
            "exit_code": 1,
            "counts": {},
            "output_tail": "\n".join(lines),
        }
    ]
    [row] = checks_evidence(results)
    assert "[truncated" in row["output"]
    assert "line 0" not in row["output"]
    assert "line 24" in row["output"]


def test_checks_evidence_unrunnable_carries_the_command_and_error() -> None:
    results = [
        {
            "name": "ghost",
            "cmd": "true",
            "passed": False,
            "outcome": "unrunnable",
            "error": "No such file or directory",
            "exit_code": None,
            "counts": {},
            "output_tail": "",
        }
    ]
    [row] = checks_evidence(results)
    assert "cmd: true" in row["output"]
    assert "No such file or directory" in row["output"]


def test_checks_evidence_unrunnable_appends_captured_output_beside_the_error() -> None:
    results = [
        {
            "name": "tests",
            "cmd": "no-such-binary -q",
            "passed": False,
            "outcome": "unrunnable",
            "error": "command not found: no-such-binary -q",
            "exit_code": 127,
            "counts": {},
            "output_tail": "/bin/sh: 1: no-such-binary: not found",
        }
    ]
    [row] = checks_evidence(results)
    assert "command not found: no-such-binary -q" in row["output"]
    assert "/bin/sh: 1: no-such-binary: not found" in row["output"]


def test_all_passed() -> None:
    assert all_passed([{"passed": True}, {"passed": True}]) is True
    assert all_passed([{"passed": True}, {"passed": False}]) is False
    assert all_passed([]) is True


# ---------------------------------------------------------------------------
# quarantine_reason
# ---------------------------------------------------------------------------


def test_quarantine_reason_is_none_when_everything_passed() -> None:
    assert quarantine_reason([{"passed": True, "outcome": "passed"}]) is None


def test_quarantine_reason_is_the_harness_fault_wording_for_an_unrunnable_check() -> None:
    results = [{"name": "ghost", "passed": False, "outcome": "unrunnable", "error": "No such file or directory"}]
    reason = quarantine_reason(results)
    assert reason == "harness fault: check 'ghost' could not run: No such file or directory"


def test_quarantine_reason_is_the_ordinary_wording_plus_see_evidence_for_a_real_failure() -> None:
    results = [{"name": "tests", "passed": False, "outcome": "failed"}]
    assert quarantine_reason(results) == "configured checks failed: tests — see evidence"


def test_quarantine_reason_does_not_hide_a_real_failure_behind_an_unrunnable_one() -> None:
    results = [
        {"name": "tests", "passed": False, "outcome": "failed"},
        {"name": "lint", "passed": False, "outcome": "unrunnable", "error": "No such file or directory"},
    ]
    reason = quarantine_reason(results)
    assert reason == "configured checks failed: tests, lint — see evidence"
    assert not is_harness_fault(reason)


# ---------------------------------------------------------------------------
# is_harness_fault
# ---------------------------------------------------------------------------


def test_is_harness_fault_matches_the_prefix_quarantine_reason_emits() -> None:
    reason = quarantine_reason([{"name": "ghost", "passed": False, "outcome": "unrunnable", "error": "boom"}])
    assert is_harness_fault(reason)


def test_is_harness_fault_is_false_for_the_ordinary_wording() -> None:
    assert not is_harness_fault("configured checks failed: tests — see evidence")


# ---------------------------------------------------------------------------
# create_worktree
# ---------------------------------------------------------------------------


def test_create_worktree_creates_it_at_head(tmp_path) -> None:
    repo = _init_repo(tmp_path / "repo")
    worktree = tmp_path / "wt"
    ok, detail = create_worktree(repo, worktree, branch="agents/run-1")
    assert ok, detail
    assert (worktree / "a.txt").read_text(encoding="utf-8") == "one\n"
    assert (worktree / ".git").exists()


def test_create_worktree_existing_branch_fails_with_gits_message(tmp_path) -> None:
    """The caller picks unique branch names; guessing a suffix here would
    silently attach the work to the wrong branch, so this just surfaces
    git's own refusal."""
    repo = _init_repo(tmp_path / "repo")
    _run(["git", "branch", "taken"], cwd=repo)
    ok, detail = create_worktree(repo, tmp_path / "wt", branch="taken")
    assert not ok
    assert "taken" in detail


def test_create_worktree_base_ref_respected(tmp_path) -> None:
    """Two commits; basing on the first must produce the FIRST commit's file
    state in the new worktree, not the second's."""
    repo = _init_repo(tmp_path / "repo")
    first = _head(repo)
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    _run(["git", "add", "a.txt"], cwd=repo)
    _run(["git", "commit", "-q", "-m", "second"], cwd=repo)

    worktree = tmp_path / "wt"
    ok, detail = create_worktree(repo, worktree, branch="agents/based", base=first)
    assert ok, detail
    assert (worktree / "a.txt").read_text(encoding="utf-8") == "one\n"


# ---------------------------------------------------------------------------
# integration: create_worktree + apply_patch + run_checks see the SAME state
# ---------------------------------------------------------------------------


def test_checks_run_against_the_applied_state_not_the_pre_patch_one(tmp_path) -> None:
    """The entire point of the check arm: a check that greps for the patched
    line must find it, which only happens if apply_patch actually landed the
    diff in the same worktree run_checks executes in."""
    repo = _init_repo(tmp_path / "repo", filename="status.txt", content="pending\n")
    worktree = tmp_path / "wt"
    ok, detail = create_worktree(repo, worktree, branch="agents/patch-run")
    assert ok, detail

    # Produce a real unified diff by editing inside the worktree and asking git
    # for it, then undo the edit — the diff is applied for real by apply_patch,
    # the thing under test, not by this edit.
    (worktree / "status.txt").write_text("patched\n", encoding="utf-8")
    diff = subprocess.run(["git", "diff"], cwd=worktree, capture_output=True, text=True).stdout
    assert diff, "expected git to produce a non-empty diff"
    _run(["git", "checkout", "--", "status.txt"], cwd=worktree)
    assert (worktree / "status.txt").read_text(encoding="utf-8") == "pending\n"

    applied_ok, applied_detail = apply_patch(diff, worktree)
    assert applied_ok, applied_detail
    assert (worktree / "status.txt").read_text(encoding="utf-8") == "patched\n"

    checks = [
        {
            "name": "grep-patched",
            "cmd": _py(
                """
                import sys
                content = open('status.txt').read()
                if 'patched' in content:
                    print('1 passed')
                    sys.exit(0)
                print('1 failed')
                sys.exit(1)
                """
            ),
        }
    ]
    [result] = run_checks(worktree, checks)
    assert result["passed"] is True
    assert result["counts"] == {"passed": 1}


def test_repo_checks_ignores_comments_and_blanks_and_keeps_order():
    text = textwrap.dedent(
        """
        # a leading comment
        pytest -q

        ruff check .
        # a trailing comment
        """
    )
    assert repo_checks(text) == [
        {"name": "pytest", "cmd": "pytest -q"},
        {"name": "ruff", "cmd": "ruff check ."},
    ]


def test_repo_checks_collapses_a_duplicate_cmd_to_its_first_occurrence():
    text = "pytest -q\nruff check .\npytest -q\n"
    assert repo_checks(text) == [
        {"name": "pytest", "cmd": "pytest -q"},
        {"name": "ruff", "cmd": "ruff check ."},
    ]


def test_repo_checks_names_are_the_first_word():
    assert repo_checks("mypy src/ --strict") == [{"name": "mypy", "cmd": "mypy src/ --strict"}]


def test_repo_checks_never_raises_on_odd_input():
    assert repo_checks("") == []
    assert repo_checks(None) == []
