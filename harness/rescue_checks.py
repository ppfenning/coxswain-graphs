"""Run the cartridge's checks on a kept patch in a fresh worktree of its base, no model involved."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from harness.checks import HARNESS_FAULT_PREFIX, all_passed, quarantine_reason, run_checks

__all__ = ["evidence_rows", "verify_patch"]

_TAIL_LINES = 40
_SOURCE = "harness_verify"


def _exit_note(result: Mapping[str, Any]) -> str:
    code = result.get("exit_code")
    if code is not None:
        return f"(exit {code})"
    if result.get("error"):
        return "(could not run, no exit code)"
    return "(timed out, no exit code)"


def evidence_rows(results: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    """One `harness_verify` row per check result: last 40 lines of output, then the exit."""
    rows: list[dict[str, str]] = []
    for result in results:
        tail = str(result.get("output_tail") or "")
        error = str(result.get("error") or "")
        body = f"{error}\n{tail}" if error and tail else error or tail
        kept = "\n".join(body.splitlines()[-_TAIL_LINES:])
        rows.append(
            {
                "command": str(result.get("cmd")),
                "output": f"{kept}\n{_exit_note(result)}",
                "source": _SOURCE,
            }
        )
    return rows


def _failure_reason(results: Sequence[Mapping[str, Any]]) -> str:
    """A check that could not run outranks a real failure beside it: the fault is named first."""
    unrunnable = [r for r in results if r.get("outcome") == "unrunnable"]
    return quarantine_reason(unrunnable or results) or "configured check failed"


def _outcome(applied: bool, passed: bool, rows: list[dict[str, str]], reason: str) -> dict[str, Any]:
    return {"applied": applied, "passed": passed, "rows": rows, "reason": reason}


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def _verify_in(
    worktree: Path,
    patch: str,
    checks: Sequence[Mapping[str, Any]],
    apply: Callable[[Path, str], str | None],
) -> dict[str, Any]:
    try:
        error = apply(worktree, patch)
    except Exception as exc:  # the injected apply is foreign code; its crash is a fault, not a verdict
        return _outcome(False, False, [], f"{HARNESS_FAULT_PREFIX} apply raised: {exc!r}")
    if error is not None:
        return _outcome(False, False, [], f"patch did not apply: {error}")
    try:
        results = run_checks(worktree, checks)
    except ValueError as exc:
        return _outcome(True, False, [], f"{HARNESS_FAULT_PREFIX} {exc}")
    rows = evidence_rows(results)
    if all_passed(results):
        return _outcome(True, True, rows, "all checks passed")
    return _outcome(True, False, rows, _failure_reason(results))


def verify_patch(
    repo: Path,
    base_ref: str,
    patch: str,
    checks: Sequence[Mapping[str, Any]],
    *,
    apply: Callable[[Path, str], str | None],
    workdir: Path,
) -> dict[str, Any]:
    """Empty `checks` is refused, since nothing run is not evidence; the worktree is always removed."""
    if not checks:
        return _outcome(False, False, [], "no checks configured")
    workdir.mkdir(parents=True, exist_ok=True)
    worktree = Path(tempfile.mkdtemp(prefix="rescue-", dir=workdir))
    try:
        added = _git(repo, "worktree", "add", "--detach", str(worktree), base_ref)
        if added.returncode != 0:
            return _outcome(False, False, [], f"{HARNESS_FAULT_PREFIX} worktree add failed: {added.stderr.strip()}")
        return _verify_in(worktree, patch, checks, apply)
    finally:
        _git(repo, "worktree", "remove", "--force", str(worktree))
        shutil.rmtree(worktree, ignore_errors=True)
        _git(repo, "worktree", "prune")
