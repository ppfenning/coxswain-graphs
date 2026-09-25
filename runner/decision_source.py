"""Where a router decision comes from, and the one safe way to ask for it.

The real source is supplied by whoever composes the graph run, above graphs.
A graph and a runner only see the protocol, so a failing source never fails a node.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from runner.decision_log import RouterDecision
from runner.tier_resolution import Hints

__all__ = ["DecisionSource", "NoDecisionSource", "ask"]


@runtime_checkable
class DecisionSource(Protocol):
    """Returns a router decision for a role, or None. Hints may be None, as in NodeRunner.run."""

    def __call__(self, role: str, hints: Hints | None) -> RouterDecision | None: ...


class NoDecisionSource:
    """The source that never has a decision."""

    def __call__(self, role: str, hints: Hints | None) -> RouterDecision | None:
        return None


def ask(source: DecisionSource, role: str, hints: Hints | None) -> RouterDecision | None:
    """The source's decision, or None when the source raises."""
    try:
        decision = source(role, hints)
    except Exception:  # a failing source must never fail a node
        return None
    else:
        return decision
