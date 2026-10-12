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

import contextlib
import os
import re
import shlex
import signal
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

__all__ = [
    "HARNESS_FAULT_PREFIX",
    "all_passed",
    "check_feedback",
    "check_outcome",
    "check_timeout",
    "checks_evidence",
    "collected_ids",
    "coverage_floor_holds",
    "failed_test_files",
    "failures_outside",
    "fixable_checks",
    "is_harness_fault",
    "outside_surface_files",
    "quarantine_reason",
    "refix_route",
    "repo_checks",
    "run_checks",
]

HARNESS_FAULT_PREFIX = "harness fault:"
_OUTSIDE_SURFACES_PREFIX = "configured check failed outside surfaces: "
_FAILED_LINE_RE = re.compile(r"^FAILED (\S+?)::", re.MULTILINE)
_SUMMARY_HEADER = "short test summary info"

# Tokens like "12 passed", "2 failed", "1 error"/"errors", "3 skipped". Generic
# on purpose: it reads whatever a test runner prints rather than special-casing
# pytest, go test, jest, and every other framework's own vocabulary.
_COUNT_RE = re.compile(r"(\d+)\s+(passed|failed|error|errors|skipped)\b", re.IGNORECASE)

_TAIL_CHARS = 2000
_REAP_SECONDS = 5  # how long a killed check's pipes get to drain before they are abandoned
_TAIL_LINES = 20
_TRUNCATION_MARKER = f"... [truncated to last {_TAIL_LINES} lines]"
_LINT_NAMES = frozenset({"lint", "ruff"})

# pytest's own exit code for "the command ran, but collected zero tests" —
# distinct from a real failure, which exits 1, and from a collection error,
# which exits 2 or 4.
_PYTEST_NO_TESTS_EXIT_CODE = 5
_SKIPPED_NO_PYTHON_TESTS = "skipped: no Python tests"
_TEST_FILE_GLOBS = ("test_*.py", "*_test.py")
_EXCLUDED_DIR_NAMES = frozenset({".venv", "target", "node_modules"})


def _is_pytest_cmd(cmd: str) -> bool:
    """Whether `cmd`'s own program is pytest, not merely a command that mentions it.

    Matches `pytest -q` and any absolute or relative path ending `/pytest`;
    a command that happens to print the word "pytest" does not count.
    """
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        return False
    return bool(tokens) and Path(tokens[0]).name == "pytest"


def _has_python_tests(worktree: Path) -> bool:
    """True iff `worktree` holds a `test_*.py` or `*_test.py` file, at any depth.

    A match under a `.venv`, `target`, or `node_modules` directory component
    does not count — those are vendored or built trees, not the repository's
    own tests.
    """
    for pattern in _TEST_FILE_GLOBS:
        for path in worktree.rglob(pattern):
            if not path.is_file():
                continue
            parent_parts = path.relative_to(worktree).parts[:-1]
            if _EXCLUDED_DIR_NAMES.isdisjoint(parent_parts):
                return True
    return False


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


def collected_ids(output: str) -> set[str]:
    """Node ids from `pytest --collect-only -q` stdout, pure.

    A node id line carries `::`; the trailing summary line ("N tests
    collected", "no tests ran") does not, so it is excluded by the same test.
    """
    return {line.strip() for line in output.splitlines() if "::" in line}


def coverage_floor_holds(before: set[str], after: set[str]) -> bool:
    """True iff every id collected `before` a trim is still collected `after`."""
    return before <= after


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


def fixable_checks(checks: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Checks carrying a `fix` command, reshaped to `{name, cmd}` for `run_checks`.

    A check without `fix` is not a candidate. The `fix` string runs verbatim —
    `--unsafe-fixes` or any other flag is never added on its behalf. A check's
    `timeout` is carried into its fix entry; no key is added when it has none.
    """
    return [
        {"name": c["name"], "cmd": c["fix"], **({"timeout": c["timeout"]} if "timeout" in c else {})}
        for c in checks
        if c.get("fix")
    ]


def check_timeout(check: Mapping[str, Any], default: int) -> int:
    """The check's own `timeout` in seconds, else `default`; a bool, non-int or non-positive value is refused."""
    if "timeout" not in check:
        return default
    value = check["timeout"]
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"check entry {check.get('name')!r} has a bad 'timeout' (need a positive int): {value!r}")
    return value


def _text(value: str | bytes | None) -> str:
    """A partial stream as text: `TimeoutExpired` hands back bytes even when the run asked for text."""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value or ""


def _kill_group(proc: subprocess.Popen[str]) -> None:
    """SIGKILL the check's whole process group; a group that is already gone is fine."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)


def _kill_and_collect(proc: subprocess.Popen[str], first: subprocess.TimeoutExpired) -> str:
    """Kill a timed-out check's group and return the output it produced.

    The second `communicate()` returns everything read so far, so the output on
    `first` is only the fallback. It is bounded: a grandchild that left the
    group (`setsid`) survives the kill and would hold the pipes open forever.
    """
    _kill_group(proc)
    try:
        out, err = proc.communicate(timeout=_REAP_SECONDS)
    except subprocess.TimeoutExpired as late:
        for pipe in (proc.stdout, proc.stderr):
            pipe.close()
        proc.wait()
        out, err = late.stdout, late.stderr
    collected = _text(out) + _text(err)
    return collected if collected else _text(first.stdout) + _text(first.stderr)


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
        limit = check_timeout(check, timeout)

        try:
            # Own session so a timeout can kill the shell's grandchildren too;
            # a child left alive keeps the pipes open and communicate() hangs.
            proc = subprocess.Popen(
                cmd,
                shell=True,
                cwd=worktree,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            try:
                stdout, stderr = proc.communicate(timeout=limit)
            except subprocess.TimeoutExpired:
                raise
            except BaseException:
                # `subprocess.run` killed its child on any error; so must this.
                _kill_group(proc)
                proc.wait()
                raise
        except subprocess.TimeoutExpired as exc:
            partial = _kill_and_collect(proc, exc)
            results.append(
                {
                    "name": name,
                    "cmd": cmd,
                    "passed": False,
                    "outcome": "timed_out",
                    "error": f"{HARNESS_FAULT_PREFIX} check {name!r} timed out after {limit}s",
                    "exit_code": None,
                    "counts": _parse_counts(partial),
                    "output_tail": (partial + f"\n[timed out after {limit}s]")[-_TAIL_CHARS:],
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

        combined = (stdout or "") + (stderr or "")
        not_found = proc.returncode == 127
        error = f"command not found: {cmd}" if not_found else None
        no_tests_ran = (
            error is None
            and proc.returncode == _PYTEST_NO_TESTS_EXIT_CODE
            and _is_pytest_cmd(cmd)
            and not _has_python_tests(worktree)
        )
        passed = True if no_tests_ran else proc.returncode == 0
        outcome = "skipped" if no_tests_ran else check_outcome(proc.returncode, error)
        results.append(
            {
                "name": name,
                "cmd": cmd,
                "passed": passed,
                "outcome": outcome,
                "error": error,
                "exit_code": proc.returncode,
                "counts": _parse_counts(combined),
                "output_tail": combined[-_TAIL_CHARS:],
            }
        )
    return results


def checks_evidence(
    results: Sequence[Mapping[str, Any]], *, prefix: str = "checks", always_tail: bool = False
) -> list[dict[str, str]]:
    """Map check results to the `{check, output}` evidence-row shape.

    `prefix` names the row family. `always_tail` keeps the output of a passing
    result too, for a command whose output is the evidence itself.

    Verdict first, counts when parsed, exit code always — the same order a
    reader scans a CI summary in, and the same discipline as every other
    evidence row in this system: a claim without the numbers behind it is a
    guess with formatting.
    """
    rows: list[dict[str, str]] = []
    for result in results:
        if result.get("outcome") == "skipped":
            rows.append({"check": f"{prefix}:{result['name']}", "output": _SKIPPED_NO_PYTHON_TESTS})
            continue
        counts = result.get("counts") or {}
        counted = ", ".join(f"{v} {k}" for k, v in counts.items())
        verdict = "pass" if result.get("passed") else "FAIL"
        detail = f"{counted} " if counted else ""
        summary = f"{verdict} — {detail}(exit {result.get('exit_code')})"
        captured = result.get("output_tail") or ""
        if result.get("outcome") == "unrunnable":
            error_text = result.get("error") or ""
            body = f"{error_text}\n{captured}" if captured else error_text
        else:
            body = captured
        tail = f"\ncmd: {result.get('cmd')}\n{_tail_lines(body)}"
        carries_tail = always_tail or not result.get("passed")
        output = summary + tail if carries_tail else summary
        rows.append({"check": f"{prefix}:{result['name']}", "output": output})
    return rows


def all_passed(results: Sequence[Mapping[str, Any]]) -> bool:
    return all(r.get("passed") for r in results)


def is_harness_fault(reason: str) -> bool:
    return reason.startswith(HARNESS_FAULT_PREFIX)


def check_feedback(results: Sequence[Mapping[str, Any]]) -> str:
    """The failing checks' name, command, exit code and output tail, for a builder to read."""
    return "\n\n".join(
        f"{r.get('name')}: {r.get('cmd')}\nexit {r.get('exit_code')}\n{_tail_lines(str(r.get('output_tail') or ''))}"
        for r in results
        if not r.get("passed")
    )


def refix_route(
    results: Sequence[Mapping[str, Any]],
    checks: Sequence[Mapping[str, Any]],
    *,
    attempts_left: float,
    style_bound: bool,
) -> str:
    """Where an approved build whose checks failed goes next: quarantine, style_pass or revise.

    Only a real failure re-enters, and only while an attempt remains. A failure
    counts as style-only when every failing check has a `fix` command or is
    named lint or ruff, and the `style_pass` seat is bound.
    """
    reason = quarantine_reason(results)
    if reason is None or is_harness_fault(reason) or attempts_left <= 0:
        return "quarantine"
    lint = {c["name"] for c in checks if c.get("fix") or c["name"] in _LINT_NAMES}
    if style_bound and all(r["name"] in lint for r in results if not r.get("passed")):
        return "style_pass"
    return "revise"


def _norm(path: str) -> str:
    return path.removeprefix("./").rstrip("/")


def _covers(path: str, root: str) -> bool:
    """Path equality or directory prefix; `tests` covers `tests/a.py` but not `tests_extra/a.py`."""
    return root in ("", ".") or path == root or path.startswith(root + "/")


def failed_test_files(output: str) -> tuple[str, ...]:
    """Sorted unique file paths from pytest `FAILED <file>::<test>` lines."""
    return tuple(sorted({_norm(f) for f in _FAILED_LINE_RE.findall(output)}))


def failures_outside(output: str, surfaces: Sequence[str], patch_files: Sequence[str]) -> tuple[str, ...]:
    """The failing files when all lie outside both surfaces and patch files, else ().

    No roots means nothing to be outside of, and an absolute path cannot be placed: both give ().
    """
    files = failed_test_files(output)
    roots = tuple(_norm(r) for r in (*surfaces, *patch_files))
    if not roots or any(f.startswith("/") or any(_covers(f, r) for r in roots) for f in files):
        return ()
    return files


def outside_surface_files(reason: str) -> tuple[str, ...]:
    """The file list from a `configured check failed outside surfaces: ...` reason, else ()."""
    if not reason.startswith(_OUTSIDE_SURFACES_PREFIX):
        return ()
    return tuple(f for f in reason[len(_OUTSIDE_SURFACES_PREFIX):].split(", ") if f)


def quarantine_reason(
    results: Sequence[Mapping[str, Any]],
    surfaces: Sequence[str] = (),
    patch_files: Sequence[str] = (),
) -> str | None:
    if all_passed(results):
        return None
    faults = ("unrunnable", "timed_out")
    real_failures = [r for r in results if not r.get("passed") and r.get("outcome") not in faults]
    if not real_failures:
        fault = next(r for r in results if r.get("outcome") in faults)
        if fault.get("outcome") == "timed_out":
            return str(fault.get("error"))
        return f"{HARNESS_FAULT_PREFIX} check '{fault['name']}' could not run: {fault.get('error')}"
    failing = [r for r in results if not r.get("passed")]
    tails = [str(r.get("output_tail") or "") for r in failing]
    # Each failing check's tail must hold pytest's whole summary block; otherwise a
    # failure it cannot see (a truncated FAILED line, a lint check) may be in the surfaces.
    complete = all(_SUMMARY_HEADER in t for t in tails)
    outside = failures_outside("\n".join(tails), surfaces, patch_files) if complete else ()
    if outside:
        return f"{_OUTSIDE_SURFACES_PREFIX}{', '.join(outside)}"
    names = ", ".join(r["name"] for r in failing)
    first_line = next((ln for ln in str(failing[0].get("output_tail") or "").splitlines() if ln.strip()), "")
    detail = f" — {first_line}" if first_line else ""
    return f"configured check failed: {names}{detail}"
