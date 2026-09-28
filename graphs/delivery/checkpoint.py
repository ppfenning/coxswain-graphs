"""The checkpoint call: resume the build or send it back, and why.

A pure core with no clock, no filesystem, and no network: every fact the
call needs arrives in `CheckpointSignals`, and the answer is a `Decision`
value rather than a side effect. This lets a checkpoint be judged the same
way a unit test judges a pure function — literal signals in, one of two
literal shapes out — and keeps the decision testable without a session, a
budget stop, or a partial patch to construct.

The rule order follows `lifecycle_propose.py`'s `_continue_ok`: a fixed
sequence of checks, first match returns, each a different reason so whoever
reads the `Revise` acts on the text. This module does not import or call
`_continue_ok` — the two serve different callers and are free to diverge.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "CheckpointSignals",
    "Decision",
    "Resume",
    "Revise",
    "checkpoint_decision",
    "checkpoint_ledger_line",
]


@dataclass(frozen=True)
class CheckpointSignals:
    """What a checkpoint knows about the build it is judging."""

    spend_usd: float
    guide_usd: float
    checkpoint_index: int
    turns: int
    diff_grew: bool
    checks_pass: bool
    files_outside_surfaces: tuple[str, ...]


@dataclass(frozen=True)
class Resume:
    """Let the build continue to its next checkpoint."""


@dataclass(frozen=True)
class Revise:
    """Stop the build; `reason` names the remedy for whoever reads it."""

    reason: str


Decision = Resume | Revise


def checkpoint_decision(signals: CheckpointSignals, fractions: tuple[float, ...]) -> Decision:
    """Resume, or revise with the reason. Ordered rules, first match wins.

    1. Partial work outside the task's surfaces: re-scope.
    2. No growth since the previous checkpoint: re-ground.
    3. The last checkpoint reached with checks still failing: split.
    4. Otherwise resume.
    """
    if signals.files_outside_surfaces:
        first = signals.files_outside_surfaces[0]
        return Revise(f"re-scope: partial work touches {first} outside the task's surfaces")
    if not signals.diff_grew:
        return Revise("re-ground: no change since the previous checkpoint")
    if signals.checkpoint_index == len(fractions) - 1 and not signals.checks_pass:
        return Revise("split: runaway checkpoint reached with the named checks still failing")
    return Resume()


def checkpoint_ledger_line(*, ts: str, task: str, role: str, signals: CheckpointSignals, decision: Decision) -> dict[str, Any]:
    """One ledger record for a checkpoint call: signals plus the decision reached on them.

    A `Revise` carries its `reason`; a `Resume` carries no `reason` key at all,
    rather than one holding `None` — the ledger's reader should not have to
    tell "no reason" apart from "reason not yet filled in".
    """
    line = {
        "ts": ts,
        "task": task,
        "role": role,
        "checkpoint_index": signals.checkpoint_index,
        "spend_usd": signals.spend_usd,
        "guide_usd": signals.guide_usd,
        "turns": signals.turns,
        "diff_grew": signals.diff_grew,
        "checks_pass": signals.checks_pass,
        "files_outside_surfaces": list(signals.files_outside_surfaces),
        "decision": "resume" if isinstance(decision, Resume) else "revise",
    }
    return line if isinstance(decision, Resume) else {**line, "reason": decision.reason}
