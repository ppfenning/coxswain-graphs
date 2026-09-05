"""Patch application, in a worktree the harness owns."""

from __future__ import annotations

import subprocess
from pathlib import Path

__all__ = ["apply_patch", "create_worktree", "normalise_patch"]

_TRAILING_MARKERS = ("</patch>", "</diff>", "</code>", "```")
_MIDDLE_TAGS = ("</patch>", "</diff>", "<patch>", "<diff>")


def normalise_patch(text: str) -> tuple[str, str | None]:
    """Strip one trailing markup marker a structured-output builder can leak.

    A build node's patch field is prose to whatever produced it, and prose
    sometimes arrives wrapped in the tags or fences the model used to talk
    about it — a closing `</patch>` glued to the last content line, or a
    stray ``` fence. `git apply` does not know that convention and fails on
    it, or worse, applies it and leaves the tag in the file. This strips
    exactly one such marker from the end, then refuses rather than guesses
    if markup is still present anywhere else: a tag mid-patch means the
    whole shape is suspect, not just its last line.
    """
    if not text:
        return text, None
    rstripped = text.rstrip("\n")
    marker = next((m for m in _TRAILING_MARKERS if rstripped.endswith(m)), None)
    # The wrong belief this trades on: that a trailing fence or tag is always
    # leaked markup and never content a hunk actually meant to add. It is
    # stripped unconditionally on that belief, same shape as the doctor
    # incident this exists for — the cost of the trade is a hunk whose true
    # last line ends the same way, which this cannot tell from a leak.
    body = rstripped[: -len(marker)].rstrip(" \t\n") if marker else rstripped
    if marker and not body:
        # A fence or tag with nothing under it is not an empty change, it is a
        # builder that returned markup instead of a patch; letting it through
        # would take the --allow-empty path and record "applied" over an
        # unchanged tree.
        return text, "builder output was markup only: no patch under the marker"
    cleaned = body + "\n" if body else ""
    for lineno, line in enumerate(cleaned.splitlines(), start=1):
        # Column zero, not substring: every diff line carries a `+`, `-`, or
        # space prefix, and every header starts with a diff keyword, so only
        # an actual tag or fence line — never a hunk that mentions one as
        # content — starts at column zero with `<` or a backtick fence.
        is_tag = line.startswith("<") and line.rstrip() in _MIDDLE_TAGS
        is_fence = line.startswith("```") and (line.rstrip() == "```" or line.startswith("```diff"))
        if is_tag or is_fence:
            return text, f"builder output contained markup at line {lineno}: {line}"
    return cleaned, None


def apply_patch(patch: str, worktree: Path) -> tuple[bool, str]:
    """Apply the build node's diff inside the worktree the harness owns.

    The one place anything in this system writes to a working tree, and it is
    the harness doing it — not the node that produced the patch. git apply
    needs a work tree, so the harness makes one it owns rather than applying
    anywhere near a real checkout: an agent that owns a worktree can be wrong
    destructively without costing anything.
    """
    cleaned, refusal = normalise_patch(patch)
    if refusal is not None:
        return False, refusal
    worktree.mkdir(parents=True, exist_ok=True)
    if not (worktree / ".git").exists():
        subprocess.run(["git", "init", "-q"], cwd=worktree, capture_output=True, text=True)
    # A diff that arrives through a JSON field can lose its final newline,
    # and git rejects such a patch as corrupt at its last line even though
    # every hunk is intact. Restoring the newline changes no hunk; it only
    # lets git read the one it already has. --recount covers the sibling
    # case: a hunk header whose line count is off by one still applies,
    # because git recomputes the count from the hunk body instead of
    # trusting the header. It does not relax context matching, so a hunk
    # whose content does not fit the file still fails with git's own message.
    normalised = cleaned if cleaned.endswith("\n") or not cleaned else cleaned + "\n"
    result = subprocess.run(
        ["git", "apply", "--verbose", "--allow-empty", "--recount", "-"],
        input=normalised,
        cwd=worktree,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0, (result.stderr or result.stdout).strip()


def create_worktree(repo: Path, worktree: Path, *, branch: str, base: str | None = None) -> tuple[bool, str]:
    """Check out a real worktree OF the project, not a scratch dir beside it.

    The scratch `git init` directory `apply_patch` falls back to can hold a
    diff, but it has no source tree behind it — no dependencies installed, no
    project config, nothing a check command could run against. Checks need the
    project, so they need a worktree of the actual repo, and the harness owns
    this one for the same reason it owns the scratch one: the one place
    anything here writes to a working tree is the harness, never the node that
    produced the patch.

    If `branch` already exists, this fails with git's own message rather than
    guessing a suffix — the caller is responsible for choosing a unique name,
    because silently renaming it would attach the work to the wrong branch
    without anyone deciding that.
    """
    cmd = ["git", "-C", str(repo), "worktree", "add", "-b", branch, str(worktree)]
    if base is not None:
        cmd.append(base)
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0, (result.stderr or result.stdout).strip()
