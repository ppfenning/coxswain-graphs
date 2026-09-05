"""apply_patch: the harness's one write to a working tree."""

from __future__ import annotations

import subprocess
from pathlib import Path

from harness.worktree import apply_patch, normalise_patch


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
    assert [p for p in tmp_path.iterdir() if p.name != ".git"] == []


def test_a_hunk_header_that_overcounts_by_one_still_applies(tmp_path: Path) -> None:
    """error: corrupt patch at <stdin>:256 twice cost an approved run its patch.

    Built from a one-line modification, the shape of the incident: the final
    hunk of a real diff overcounted, not a file-creation hunk with no context
    to match against.
    """
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "f.txt").write_text("one\ntwo\nthree\n")
    diff = (
        "diff --git a/f.txt b/f.txt\n"
        "--- a/f.txt\n"
        "+++ b/f.txt\n"
        "@@ -1,3 +1,3 @@\n"
        " one\n"
        "-two\n"
        "+TWO\n"
        " three\n"
    )
    doctored = diff.replace("@@ -1,3 +1,3 @@", "@@ -1,3 +1,4 @@")
    assert doctored != diff
    ok, detail = apply_patch(doctored, tmp_path)
    assert ok, detail
    assert (tmp_path / "f.txt").read_text() == "one\nTWO\nthree\n"


def test_a_trailing_tag_glued_to_the_last_line_is_stripped(tmp_path: Path) -> None:
    """tools-setup-doctor-2: the closing tag ate the newline, not just the line."""
    diff = _diff_for(tmp_path, "new.txt", "one\ntwo\n")
    glued = diff.rstrip("\n") + "</patch>"
    ok, detail = apply_patch(glued, tmp_path)
    assert ok, detail
    assert (tmp_path / "new.txt").read_text() == "one\ntwo\n"


def test_a_trailing_fence_is_stripped(tmp_path: Path) -> None:
    diff = _diff_for(tmp_path, "new.txt", "one\ntwo\n")
    fenced = diff + "```\n"
    ok, detail = apply_patch(fenced, tmp_path)
    assert ok, detail
    assert (tmp_path / "new.txt").read_text() == "one\ntwo\n"


def test_a_clean_patch_round_trips_unchanged() -> None:
    diff = (
        "diff --git a/f.txt b/f.txt\n"
        "--- a/f.txt\n"
        "+++ b/f.txt\n"
        "@@ -1,1 +1,1 @@\n"
        "-one\n"
        "+two\n"
    )
    cleaned, refusal = normalise_patch(diff)
    assert refusal is None
    assert cleaned == diff


def test_a_tag_that_is_itself_the_change_is_not_markup() -> None:
    """A diff that edits this file's own prompt strings must not self-refuse.

    Its added lines carry `</patch>` and `</diff>` as ordinary `+` content,
    not as tags — a real diff line always has a `+`, `-`, or space prefix.
    """
    diff = (
        "diff --git a/f.py b/f.py\n"
        "--- a/f.py\n"
        "+++ b/f.py\n"
        "@@ -1,1 +1,2 @@\n"
        " context line holding </diff> as prose\n"
        '+    "a stray </patch> fails the checks."\n'
    )
    cleaned, refusal = normalise_patch(diff)
    assert refusal is None
    assert cleaned == diff


def test_markup_in_the_middle_is_refused_and_git_never_runs(tmp_path: Path) -> None:
    diff = (
        "diff --git a/f.txt b/f.txt\n"
        "--- a/f.txt\n"
        "+++ b/f.txt\n"
        "</patch>\n"
        "@@ -1,1 +1,1 @@\n"
        "-one\n"
        "+two\n"
    )
    never = tmp_path / "never"
    ok, detail = apply_patch(diff, never)
    assert not ok
    assert "markup" in detail
    assert "line 4" in detail
    assert not never.exists()


def test_a_mismatched_context_line_still_fails_and_names_it(tmp_path: Path) -> None:
    """A miscounted header does not buy a mismatched hunk a pass: --recount

    only recomputes the header, it does not touch context matching.
    """
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "f.txt").write_text("one\ntwo\n")
    diff = (
        "diff --git a/f.txt b/f.txt\n"
        "--- a/f.txt\n"
        "+++ b/f.txt\n"
        "@@ -1,2 +1,2 @@\n"
        " wrong\n"
        "-two\n"
        "+three\n"
    )
    doctored = diff.replace("@@ -1,2 +1,2 @@", "@@ -1,3 +1,3 @@")
    assert doctored != diff
    ok, detail = apply_patch(doctored, tmp_path)
    assert not ok
    assert "f.txt" in detail
    assert "does not apply" in detail


def test_markup_only_output_is_refused_not_applied_as_empty(tmp_path: Path) -> None:
    """A lone fence must not collapse to an empty patch that reports success."""
    from harness.worktree import apply_patch, normalise_patch
    cleaned, refusal = normalise_patch("```\n")
    assert cleaned == "```\n" and refusal is not None and "markup only" in refusal
    ok, detail = apply_patch("</patch>\n", tmp_path / "wt")
    assert ok is False and "markup only" in detail
    assert not (tmp_path / "wt" / ".git").exists() or not any((tmp_path / "wt").glob("*.py")), "git never applied anything"
