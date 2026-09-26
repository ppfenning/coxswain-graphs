import subprocess
from pathlib import Path

import pytest

from harness.rescue_checks import evidence_rows, verify_patch

_ID = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]

# Adds new.txt. It applies on the base commit, which has a.txt and no later.txt.
_ADD_NEW = """diff --git a/new.txt b/new.txt
new file mode 100644
--- /dev/null
+++ b/new.txt
@@ -0,0 +1 @@
+patched
"""

# Edits later.txt, which exists only at HEAD, so it cannot apply on the base.
_EDIT_LATER = """diff --git a/later.txt b/later.txt
--- a/later.txt
+++ b/later.txt
@@ -1 +1 @@
-two
+three
"""

# Passes only in a worktree at the base commit with _ADD_NEW applied.
_SEES_BASE_AND_PATCH = "grep -q patched new.txt && grep -q one a.txt && test ! -e later.txt"


def _result(cmd, code, tail, error=None):
    return {
        "name": cmd.split()[0],
        "cmd": cmd,
        "passed": code == 0,
        "exit_code": code,
        "error": error,
        "output_tail": tail,
    }


def test_a_passing_result_is_one_row_with_its_exit():
    assert evidence_rows([_result("pytest -q", 0, "3 passed")]) == [
        {"command": "pytest -q", "output": "3 passed\n(exit 0)", "source": "harness_verify"}
    ]


def test_a_failing_result_carries_its_output_and_exit_code():
    (row,) = evidence_rows([_result("pytest -q", 1, "a\nFAILED x\n1 failed")])
    assert row["output"] == "a\nFAILED x\n1 failed\n(exit 1)"


def test_a_timeout_names_the_timeout():
    (row,) = evidence_rows([_result("sleep 9", None, "\n[timed out after 5s]")])
    assert "timed out after 5s" in row["output"]
    assert row["output"].endswith("(timed out, no exit code)")


def test_a_command_that_could_not_start_names_the_error_not_a_bogus_exit():
    (row,) = evidence_rows([_result("x", None, "", error="[Errno 2] No such file")])
    assert row["output"] == "[Errno 2] No such file\n(could not run, no exit code)"


def test_only_the_last_forty_lines_are_kept():
    tail = "\n".join(f"line{i}" for i in range(45))
    (row,) = evidence_rows([_result("x", 1, tail)])
    lines = row["output"].splitlines()
    assert lines[0] == "line5"
    assert len(lines) == 41


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str]:
    """A two-commit repo; returns the root and the first commit, which is not HEAD."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "a.txt").write_text("one\n")
    _git(root, "add", ".")
    _git(root, *_ID, "commit", "-q", "-m", "base")
    base = _git(root, "rev-parse", "HEAD").strip()
    (root / "later.txt").write_text("two\n")
    _git(root, "add", ".")
    _git(root, *_ID, "commit", "-q", "-m", "later")
    return root, base


def _git_apply(worktree: Path, patch: str) -> str | None:
    proc = subprocess.run(["git", "apply", "-"], cwd=worktree, input=patch, capture_output=True, text=True)
    return None if proc.returncode == 0 else proc.stderr.strip()


def _run(repo, tmp_path, checks, patch=_ADD_NEW, apply=_git_apply):
    root, base = repo
    seen: list[Path] = []

    def recording(worktree: Path, text: str) -> str | None:
        seen.append(worktree)
        return apply(worktree, text)

    workdir = tmp_path / "wt"
    result = verify_patch(root, base, patch, checks, apply=recording, workdir=workdir)
    return result, seen, workdir


def _assert_clean(repo, seen, workdir):
    assert all(not p.exists() for p in seen)
    assert list(workdir.iterdir()) == []
    assert len(_git(repo[0], "worktree", "list").splitlines()) == 1


def test_checks_run_in_a_worktree_of_base_ref_with_the_patch_applied(repo, tmp_path):
    result, seen, workdir = _run(repo, tmp_path, [{"name": "sees", "cmd": _SEES_BASE_AND_PATCH}])
    assert (result["applied"], result["passed"], result["reason"]) == (True, True, "all checks passed")
    assert [r["source"] for r in result["rows"]] == ["harness_verify"]
    assert workdir in seen[0].parents
    _assert_clean(repo, seen, workdir)


def test_the_same_check_fails_when_the_patch_is_not_applied(repo, tmp_path):
    result, seen, workdir = _run(
        repo, tmp_path, [{"name": "sees", "cmd": _SEES_BASE_AND_PATCH}], apply=lambda w, p: None
    )
    assert (result["applied"], result["passed"]) == (True, False)
    _assert_clean(repo, seen, workdir)


def test_a_failing_check_carries_output_and_exit_code(repo, tmp_path):
    result, seen, workdir = _run(repo, tmp_path, [{"name": "bad", "cmd": "echo boom; exit 3"}])
    assert (result["applied"], result["passed"]) == (True, False)
    assert "boom" in result["rows"][0]["output"]
    assert result["rows"][0]["output"].endswith("(exit 3)")
    assert result["reason"].startswith("configured check failed")
    _assert_clean(repo, seen, workdir)


def test_a_patch_for_a_file_absent_at_base_does_not_apply(repo, tmp_path):
    result, seen, workdir = _run(repo, tmp_path, [{"name": "ok", "cmd": "true"}], patch=_EDIT_LATER)
    assert (result["applied"], result["passed"], result["rows"]) == (False, False, [])
    assert result["reason"].startswith("patch did not apply: ")
    assert "later.txt" in result["reason"]
    _assert_clean(repo, seen, workdir)


def test_a_check_that_cannot_run_is_a_harness_fault(repo, tmp_path):
    result, seen, workdir = _run(repo, tmp_path, [{"name": "nx", "cmd": "nonexistent-cmd-xyz"}])
    assert result["passed"] is False
    assert result["reason"].startswith("harness fault")
    _assert_clean(repo, seen, workdir)


def test_an_unrunnable_check_beside_a_real_failure_is_still_a_harness_fault(repo, tmp_path):
    checks = [{"name": "bad", "cmd": "exit 1"}, {"name": "nx", "cmd": "nonexistent-cmd-xyz"}]
    result, seen, workdir = _run(repo, tmp_path, checks)
    assert result["passed"] is False
    assert result["reason"].startswith("harness fault")
    assert "'nx'" in result["reason"]
    assert len(result["rows"]) == 2
    _assert_clean(repo, seen, workdir)


def test_an_apply_that_raises_is_a_harness_fault_and_cleans_up(repo, tmp_path):
    def boom(worktree: Path, patch: str) -> str | None:
        raise RuntimeError("apply crashed")

    result, seen, workdir = _run(repo, tmp_path, [{"name": "ok", "cmd": "true"}], apply=boom)
    assert (result["applied"], result["passed"], result["rows"]) == (False, False, [])
    assert result["reason"].startswith("harness fault")
    assert "apply crashed" in result["reason"]
    _assert_clean(repo, seen, workdir)


def test_a_malformed_check_entry_is_a_harness_fault_and_cleans_up(repo, tmp_path):
    result, seen, workdir = _run(repo, tmp_path, [{"name": "nocmd"}])
    assert (result["passed"], result["rows"]) == (False, [])
    assert result["reason"].startswith("harness fault")
    _assert_clean(repo, seen, workdir)


def test_empty_checks_are_refused_before_any_worktree(repo, tmp_path):
    result, seen, workdir = _run(repo, tmp_path, [])
    assert result == {"applied": False, "passed": False, "rows": [], "reason": "no checks configured"}
    assert seen == []
    assert not workdir.exists()


def test_a_bad_base_ref_is_a_harness_fault_and_leaves_nothing(repo, tmp_path):
    workdir = tmp_path / "wt"
    result = verify_patch(
        repo[0], "no-such-ref", "p", [{"name": "ok", "cmd": "true"}], apply=lambda w, p: None, workdir=workdir
    )
    assert result["reason"].startswith("harness fault")
    assert list(workdir.iterdir()) == []
