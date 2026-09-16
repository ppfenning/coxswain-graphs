"""lint_tickets — the four static checks from docs/design/work-shape.md §3.

Pure: plain data in, plain data out, no file I/O, no network, no clock.
`reach` and `coupling` are refusals; `grant` and `size` are advisory
(`Problem.severity`) — the caller in `initiative_decompose.py` routes on that.
`tree` rows may carry an optional `"imports"` list (paths that file imports),
computed once at the edge, so the coupling rule's import disjunct stays pure.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

__all__ = ["Problem", "lint_tickets"]

_REFUSAL_RULES = frozenset({"reach", "coupling"})
_ROOTED_PREFIXES = ("~/", "/")
_RISKY_SINGLE = frozenset({"cox", "uv", "gh", "ruff"})
_RISKY_TWO_WORD = frozenset({"git push"})

# docs/design/validator-reach.md §3, verbatim.
_CORPUS_DENYLIST = ("workspace/", "runs/", "*.usage.json", "ledger.jsonl", "~/.local/state")
_CORPUS_CORRECTION = "route this to `cox stats` or the chair; the build seat sees one repository worktree"


@dataclass(frozen=True)
class Problem:
    task: str
    rule: str
    detail: str
    fix: str

    @property
    def severity(self) -> str:
        """`"refusal"` for reach/coupling, `"advisory"` for grant/size."""
        return "refusal" if self.rule in _REFUSAL_RULES else "advisory"


def _tokens(text: str) -> list[str]:
    return re.findall(r"\S+", text)


def _is_notation(candidate: str) -> bool:
    """A token that is not a path at all: a bare slash between words
    ("cartridges 1.0 / graphs 1.0"), or a shape with an angle-bracket
    placeholder (`runs/<run>/tasks/...`) — the seat is describing a form,
    not naming a file it means to read."""
    stripped = candidate.strip("`,.()")
    return stripped in ("/", "~/") or "<" in stripped or ">" in stripped or not re.search(r"[\w.]", stripped)


def _looks_rooted(candidate: str) -> bool:
    """True for `~/...`, `/...`, or `workspace/...` — a rooted prefix followed
    by a real name, not a mid-path segment and not notation."""
    if _is_notation(candidate):
        return False
    stripped = candidate.strip("`,.()")
    return stripped.startswith(_ROOTED_PREFIXES) or stripped.startswith("workspace/")


def _reach_candidates(task: Mapping[str, Any]) -> list[str]:
    body_hits = [t.strip("`,.()") for t in _tokens(str(task.get("body") or "")) if _looks_rooted(t)]
    surface_hits = [s for s in task.get("surfaces") or [] if _looks_rooted(s)]
    return list(dict.fromkeys(body_hits + surface_hits))


def _looks_corpus(candidate: str) -> bool:
    """True if `candidate` names a path under a §3 corpus denylist entry."""
    if _is_notation(candidate):
        return False
    stripped = candidate.strip("`,.()")
    for pattern in _CORPUS_DENYLIST:
        if pattern.startswith("*"):
            if stripped.endswith(pattern[1:]):
                return True
        elif stripped == pattern or stripped.startswith(pattern) or stripped.endswith("/" + pattern):
            return True
    return False


def _corpus_candidates(task: Mapping[str, Any]) -> list[str]:
    body_hits = [t.strip("`,.()") for t in _tokens(str(task.get("body") or "")) if _looks_corpus(t)]
    surface_hits = [s for s in task.get("surfaces") or [] if _looks_corpus(s)]
    return list(dict.fromkeys(body_hits + surface_hits))


def _reach_problems(tasks: Sequence[Mapping[str, Any]], tree: Sequence[Mapping[str, Any]], repo: str) -> list[Problem]:
    known_paths = {str(row.get("path")) for row in tree}
    problems: list[Problem] = []
    for task in tasks:
        corpus_hits = _corpus_candidates(task)
        problems.extend(
            Problem(str(task["id"]), "reach", f"names {path}, out of the build seat's reach", _CORPUS_CORRECTION)
            for path in corpus_hits
        )
        problems.extend(
            Problem(str(task["id"]), "reach", f"names {path}, not inside {repo}", "move the artifact into the repository or drop the reference")
            for path in _reach_candidates(task)
            if path not in known_paths and path not in corpus_hits
        )
    return problems


def _is_test_surface(path: str) -> bool:
    return "test" in path.lower()


def _by_phase(tasks: Sequence[Mapping[str, Any]]) -> dict[Any, list[Mapping[str, Any]]]:
    groups: dict[Any, list[Mapping[str, Any]]] = {}
    for task in tasks:
        groups.setdefault(task.get("phase"), []).append(task)
    return groups


def _closure(by_id: Mapping[str, Mapping[str, Any]]) -> dict[str, set[str]]:
    """Every task transitively reachable from each task through `needs`."""
    memo: dict[str, set[str]] = {}

    def reach(node: str, seen: frozenset[str]) -> set[str]:
        if node in memo:
            return memo[node]
        result: set[str] = set()
        for need in by_id.get(node, {}).get("needs") or []:
            if need in seen or need not in by_id:
                continue
            result.add(need)
            result |= reach(need, seen | {need})
        memo[node] = result
        return result

    return {task_id: reach(task_id, frozenset({task_id})) for task_id in by_id}


def _shared_test_surface(a: Mapping[str, Any], b: Mapping[str, Any]) -> str | None:
    shared = sorted(set(a.get("surfaces") or []) & set(b.get("surfaces") or []))
    return next((s for s in shared if _is_test_surface(s)), None)


def _imports_of(path: str, tree_by_path: Mapping[str, Mapping[str, Any]]) -> set[str]:
    return set(tree_by_path.get(path, {}).get("imports") or [])


def _mutual_import(a: Mapping[str, Any], b: Mapping[str, Any], tree_by_path: Mapping[str, Mapping[str, Any]]) -> bool:
    """Either side's named module importing the other's is enough to couple them."""
    a_surfaces, b_surfaces = set(a.get("surfaces") or []), set(b.get("surfaces") or [])
    a_imports = {imp for s in a_surfaces for imp in _imports_of(s, tree_by_path)}
    b_imports = {imp for s in b_surfaces for imp in _imports_of(s, tree_by_path)}
    return bool((a_surfaces & b_imports) or (b_surfaces & a_imports))


def _coupling_problems(tasks: Sequence[Mapping[str, Any]], tree: Sequence[Mapping[str, Any]]) -> list[Problem]:
    by_id = {str(task["id"]): task for task in tasks}
    closure = _closure(by_id)
    tree_by_path = {str(row.get("path")): row for row in tree}
    problems: list[Problem] = []
    for group in _by_phase(tasks).values():
        for i, a in enumerate(group):
            for b in group[i + 1 :]:
                shared_test = _shared_test_surface(a, b)
                if shared_test is None and not _mutual_import(a, b, tree_by_path):
                    continue
                a_id, b_id = str(a["id"]), str(b["id"])
                if b_id in closure[a_id] or a_id in closure[b_id]:
                    continue
                detail = f"shares {shared_test} with {b_id}" if shared_test else f"{a_id} and {b_id} import one another"
                problems.append(Problem(a_id, "coupling", detail, "merge, or order with needs"))
    return problems


_LANGUAGE_TAG = re.compile(r"[A-Za-z][A-Za-z0-9_+-]*")


def _named_commands(body: str) -> list[str]:
    """One entry per backtick span, or per line of a fenced block with its language tag dropped."""
    commands: list[str] = []
    for span in re.findall(r"`([^`]+)`", body):
        lines = [line.strip() for line in span.splitlines() if line.strip()]
        if len(lines) > 1 and _LANGUAGE_TAG.fullmatch(lines[0]):
            lines = lines[1:]
        commands.extend(lines)
    return commands


def _risky_command(command: str) -> str | None:
    parts = command.split()
    if not parts:
        return None
    two_word = " ".join(parts[:2])
    if two_word in _RISKY_TWO_WORD:
        return two_word
    return parts[0] if parts[0] in _RISKY_SINGLE else None


def _grant_problems(tasks: Sequence[Mapping[str, Any]], grants: Sequence[str]) -> list[Problem]:
    allowed = set(grants or [])
    problems: list[Problem] = []
    for task in tasks:
        for command in _named_commands(str(task.get("body") or "")):
            risky = _risky_command(command)
            if risky and risky not in allowed:
                problems.append(
                    Problem(str(task["id"]), "grant", f"names `{risky}`, which is not granted", "name only pytest, git status, git diff")
                )
    return problems


def _size_problems(tasks: Sequence[Mapping[str, Any]]) -> list[Problem]:
    problems: list[Problem] = []
    for task in tasks:
        words = len(str(task.get("body") or "").split())
        if words > 700:
            problems.append(Problem(str(task["id"]), "size", f"body is {words} words", "point at a spec file in the repository"))
    return problems


def lint_tickets(
    tasks: Sequence[Mapping[str, Any]],
    tree: Sequence[Mapping[str, Any]],
    grants: Sequence[str],
    repo: str,
) -> list[Problem]:
    fixed_tasks = list(tasks)
    return [
        *_reach_problems(fixed_tasks, tree, repo),
        *_coupling_problems(fixed_tasks, tree),
        *_grant_problems(fixed_tasks, grants),
        *_size_problems(fixed_tasks),
    ]
