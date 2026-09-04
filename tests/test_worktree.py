"""apply_patch: the harness's one write to a working tree."""

from __future__ import annotations

import subprocess
from pathlib import Path

from harness.worktree import apply_patch


def _diff_for(worktree: Path, name: str, text: str) -> str:
    """A real unified diff for creating `name` with `text`, produced by git itself."""
    subprocess.run(["git", "init", "-q"], cwd=worktree, check=True)
    (worktree / name).write_text(text)
    diff = subprocess.run(
        ["git", "diff", "--no-index", "--", "/dev/null", name],
        cwd=worktree, capture_output=True, text=True,
    ).stdout
    (worktree / name).unlink()
    return diff


def test_a_patch_missing_its_final_newline_still_applies(tmp_path: Path) -> None:
    """Run 12 and 13 of a real epic lost approved patches to 'corrupt patch at <stdin>:N'."""
    diff = _diff_for(tmp_path, "new.txt", "one\ntwo\n")
    assert diff.endswith("\n")
    ok, detail = apply_patch(diff.rstrip("\n"), tmp_path)
    assert ok, detail
    assert (tmp_path / "new.txt").read_text() == "one\ntwo\n"


def test_a_well_formed_patch_is_passed_through_unchanged(tmp_path: Path) -> None:
    diff = _diff_for(tmp_path, "new.txt", "x\n")
    ok, detail = apply_patch(diff, tmp_path)
    assert ok, detail


def test_an_empty_patch_is_allowed_and_writes_nothing(tmp_path: Path) -> None:
    ok, _ = apply_patch("", tmp_path)
    assert ok
    assert list(p for p in tmp_path.iterdir() if p.name != ".git") == []
