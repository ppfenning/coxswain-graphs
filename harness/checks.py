"""Run the project's configured checks, in the worktree the harness owns.

`_change_facts` in `graphs/delivery/lifecycle_propose.py` counts a diff's shape
from the patch itself rather than asking the build node to report it, because a
node describing its own diff is describing a recollection. Checks extend the
same argument one step further: whether the tests pass is not something a
review node gets to assert either. This module runs the cartridge's configured
commands against the applied patch and turns their exit codes and output into
evidence rows — measured, never self-reported, and attached to the proposal
before the gate sees it. `repo_checks` parses a repository's own root
`.agent-checks` file into the same `{name, cmd}` shape `run_checks` expects.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

__all__ = [
    "HARNESS_FAULT_PREFIX",
    "all_passed",
    "check_outcome",
    "checks_evidence",
    "is_harness_fault",
    "quarantine_reason",
    "repo_checks",
    "run_checks",
]

HARNESS_FAULT_PREFIX = "harness fault:"

# Tokens like "12 passed", "2 failed", "1 error"/"errors", "3 skipped". Generic
# on purpose: it reads whatever a test runner prints rather than special-casing
# pytest, go test, jest, and every other framework's own vocabulary.
_COUNT_RE = re.compile(r"(\d+)\s+(passed|failed|error|errors|skipped)\b", re.IGNORECASE)

_TAIL_CHARS = 2000
_TAIL_LINES = 20
_TRUNCATION_MARKER = f"... [truncated to last {_TAIL_LINES} lines]"


def _parse_counts(output: str) -> dict[str, int]:
    """Mechanical extraction only. No match, no key — never invented."""
    counts: dict[str, int] = {}
    for number, word in _COUNT_RE.findall(output):
        key = "error" if word.lower() == "errors" else word.lower()
        counts[key] = counts.get(key, 0) + int(number)
    return counts


def _tail_lines(text: str, n: int = _TAIL_LINES) -> str:
    lines = text.splitlines()
    if len(lines) <= n:
        return text
    return "\n".join([_TRUNCATION_MARKER, *lines[-n:]])


def check_outcome(returncode: int | None, error: str | None) -> str:
    if error is not None:
        return "unrunnable"
    return "passed" if returncode == 0 else "failed"


def repo_checks(text: str) -> list[dict]:
    """Parse a `.agent-checks` file: one shell command per line, pure.

    Blank lines and lines starting with `#` are ignored. Each surviving line,
    stripped, becomes an entry whose `name` is its first whitespace-separated
    word and whose `cmd` is the stripped line itself. A duplicate `cmd` keeps
    only its first occurrence; order is otherwise preserved. Odd input
    (`None`, or anything that is not a string) yields `[]` rather than raising
    — a malformed file is a check that did not run, not a crash.
    """
    if not isinstance(text, str):
        return []
    lines = (stripped for stripped in (line.strip() for line in text.splitlines()) if stripped and not stripped.startswith("#"))
    unique = dict.fromkeys(lines)  # first occurrence wins, order preserved
    return [{"name": line.split(None, 1)[0], "cmd": line} for line in unique]


def run_checks(
    worktree: Path,
    checks: Sequence[Mapping[str, Any]],
    *,
    timeout: int = 600,
) -> list[dict[str, Any]]:
    """Execute each configured check in `worktree` and report what actually happened.

    Each entry needs `name` and `cmd`; an entry missing either is refused rather
    than silently skipped, because a check nobody ran is not a check, it is a
    gap wearing the shape of one. A command that never finishes is treated as a
    failure with no exit code — a check that hangs forever is not "still
    pending", it is a check that did not pass.
    """
    results: list[dict[str, Any]] = []
    for check in checks:
        name = check.get("name")
        cmd = check.get("cmd")
        if not name or not cmd:
            raise ValueError(f"check entry missing 'name' or 'cmd': {dict(check)!r}")

        try:
            proc = subprocess.run(
                cmd,
                shell=True,
                cwd=worktree,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            partial = ((exc.stdout or "") if isinstance(exc.stdout, str) else "") + (
                (exc.stderr or "") if isinstance(exc.stderr, str) else ""
            )
            results.append(
                {
                    "name": name,
                    "cmd": cmd,
                    "passed": False,
                    "outcome": "failed",
                    "error": None,
                    "exit_code": None,
                    "counts": _parse_counts(partial),
                    "output_tail": (partial + f"\n[timed out after {timeout}s]")[-_TAIL_CHARS:],
                }
            )
            continue
        except (FileNotFoundError, OSError) as exc:
            error = str(exc)
            results.append(
                {
                    "name": name,
                    "cmd": cmd,
                    "passed": False,
                    "outcome": check_outcome(None, error),
                    "error": error,
                    "exit_code": None,
                    "counts": {},
                    "output_tail": "",
                }
            )
            continue

        combined = (proc.stdout or "") + (proc.stderr or "")
        results.append(
            {
                "name": name,
                "cmd": cmd,
                "passed": proc.returncode == 0,
                "outcome": check_outcome(proc.returncode, None),
                "error": None,
                "exit_code": proc.returncode,
                "counts": _parse_counts(combined),
                "output_tail": combined[-_TAIL_CHARS:],
            }
        )
    return results


def checks_evidence(results: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    """Map check results to the `{check, output}` evidence-row shape.

    Verdict first, counts when parsed, exit code always — the same order a
    reader scans a CI summary in, and the same discipline as every other
    evidence row in this system: a claim without the numbers behind it is a
    guess with formatting.
    """
    rows: list[dict[str, str]] = []
    for result in results:
        counts = result.get("counts") or {}
        counted = ", ".join(f"{v} {k}" for k, v in counts.items())
        verdict = "pass" if result.get("passed") else "FAIL"
        detail = f"{counted} " if counted else ""
        summary = f"{verdict} — {detail}(exit {result.get('exit_code')})"
        tail = f"\ncmd: {result.get('cmd')}\n{_tail_lines(result.get('output_tail') or '')}"
        carries_tail = not result.get("passed") and result.get("outcome") != "unrunnable"
        output = summary + tail if carries_tail else summary
        rows.append({"check": f"checks:{result['name']}", "output": output})
    return rows


def all_passed(results: Sequence[Mapping[str, Any]]) -> bool:
    return all(r.get("passed") for r in results)


def is_harness_fault(reason: str) -> bool:
    return reason.startswith(HARNESS_FAULT_PREFIX)


def quarantine_reason(results: Sequence[Mapping[str, Any]]) -> str | None:
    if all_passed(results):
        return None
    real_failures = [r for r in results if not r.get("passed") and r.get("outcome") != "unrunnable"]
    if not real_failures:
        unrunnable = next(r for r in results if r.get("outcome") == "unrunnable")
        return f"{HARNESS_FAULT_PREFIX} check '{unrunnable['name']}' could not run: {unrunnable.get('error')}"
    failed = ", ".join(r["name"] for r in results if not r.get("passed"))
    return f"configured checks failed: {failed} — see evidence"
