"""Patch application, in a worktree the harness owns."""

from __future__ import annotations

import subprocess
from pathlib import Path

__all__ = ["apply_patch"]


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
    result = subprocess.run(
        ["git", "apply", "--verbose", "--allow-empty", "-"],
        input=patch,
        cwd=worktree,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0, (result.stderr or result.stdout).strip()
