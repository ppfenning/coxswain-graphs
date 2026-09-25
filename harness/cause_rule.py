"""Pure cause type and deterministic attempt-cause rule. No I/O, clock, model or store."""

from __future__ import annotations

from typing import Literal, get_args

Cause = Literal["ticket", "code", "review", "harness", "unknown"]
CAUSES: tuple[str, ...] = get_args(Cause)

# Measured in harness/epic.py: kinds refused|no_work|unverified|infra; reasons
# "patch did not apply: ...", "worktree <b> could not be created: ...",
# "the phase worktree could not be (re)created: ...".


def classify_cause(kind: str, reason: str) -> Cause | None:
    """Harness first, then code, then ticket; None when no rule matches."""
    if kind == "infra" or "patch did not apply" in reason:
        return "harness"
    if "worktree" in reason and "could not be" in reason:
        return "harness"
    if "configured check failed" in reason:
        return "code"
    if kind == "no_work":
        return "ticket"
    return None
