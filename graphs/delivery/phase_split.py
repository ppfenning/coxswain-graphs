"""Move same-phase needs edges into later phases.

A pure core with no I/O and no model call. The input is plain data:
`{"phases": [{"id", "goal"}, ...], "tasks": [{"id", "phase", "needs"}, ...]}`.
Other keys on the decomposition, a phase or a task are carried through unchanged.
`split_same_phase_needs` returns a new decomposition and a list of moves.
"""

from __future__ import annotations

from typing import Any

__all__ = ["split_same_phase_needs"]


def _suffix(level: int) -> str:
    # Level 0 keeps the phase id; 1 is "-b", 2 is "-c". Past "-z" is not handled.
    return "" if level == 0 else f"-{chr(ord('a') + level)}"


def _same_phase_needs(task: dict[str, Any], phase_of: dict[str, str]) -> list[str]:
    return [n for n in task.get("needs", []) if phase_of.get(n) == task["phase"]]


def _level(task_id: str, by_id: dict[str, dict[str, Any]], phase_of: dict[str, str], path: tuple[str, ...]) -> int:
    """0 with no same-phase need, else 1 + the deepest needed level. An edge back into `path` is a cycle and is ignored."""
    needed = [n for n in _same_phase_needs(by_id[task_id], phase_of) if n not in path]
    return 1 + max(_level(n, by_id, phase_of, (*path, task_id)) for n in needed) if needed else 0


def split_same_phase_needs(decomposition: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return the decomposition with same-phase edges split across `<phase>-b`, `<phase>-c`, and the moves made."""
    tasks = decomposition["tasks"]
    by_id = {t["id"]: t for t in tasks}
    phase_of = {t["id"]: t["phase"] for t in tasks}
    levels = {t["id"]: _level(t["id"], by_id, phase_of, ()) for t in tasks}

    new_tasks = [{**t, "phase": t["phase"] + _suffix(levels[t["id"]])} for t in tasks]
    moves = [
        {
            "task": t["id"],
            "from": t["phase"],
            "to": t["phase"] + _suffix(levels[t["id"]]),
            "needs": _same_phase_needs(t, phase_of),
        }
        for t in tasks
        if levels[t["id"]] > 0
    ]

    def levels_used(phase_id: str) -> list[int]:
        return sorted({levels[t["id"]] for t in tasks if t["phase"] == phase_id and levels[t["id"]] > 0})

    new_phases = [
        q
        for p in decomposition["phases"]
        for q in [dict(p), *({**p, "id": p["id"] + _suffix(k)} for k in levels_used(p["id"]))]
    ]
    return {**decomposition, "phases": new_phases, "tasks": new_tasks}, moves
