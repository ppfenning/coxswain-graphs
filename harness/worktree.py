"""Patch application, in a worktree the harness owns."""

from __future__ import annotations

import subprocess
from pathlib import Path

__all__ = ["apply_patch", "create_worktree"]


def apply_patch(patch: str, worktree: Path) -> tuple[bool, str]:
    """Apply the build node's diff inside the worktree the harness owns.

    The one place anything in this system writes to a working tree, and it is
    the harness doing it — not the node that produced the patch. git apply
    needs a work tree, so the harness makes one it owns rather than applying
    anywhere near a real checkout: an agent that owns a worktree can be wrong
    destructively without costing anything.
    """
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
    normalised = patch if patch.endswith("\n") or not patch else patch + "\n"
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
