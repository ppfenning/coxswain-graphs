"""Pure cause type and deterministic attempt-cause rule. No I/O, clock, model or store."""

from __future__ import annotations

from typing import Literal, get_args

Cause = Literal["ticket", "code", "review", "harness", "unknown"]
CAUSES: tuple[str, ...] = get_args(Cause)

# Measured in harness/epic.py: kinds refused|no_work|unverified|infra; reasons
# "patch did not apply: ...", "worktree <b> could not be created: ...",
# "the phase worktree could not be (re)created: ...".

# A node stopped by its budget, turn cap or the account's session limit did no work
# the ticket can be blamed for. Lowercase, matched against the lowercased reason. The
# last two are the banner text `LimitStop` carries (runner/claude_code_runner.py).
_LIMIT_MARKERS: tuple[str, ...] = (
    "error_max_budget_usd",
    "error_max_turns",
    "reached maximum budget",
    "you've hit your session limit",
    "usage limit",
)


def classify_cause(kind: str, reason: str) -> Cause | None:
    """A budget, turn or session limit stop first, then harness, code, ticket; None when no rule matches."""
    if any(marker in reason.lower() for marker in _LIMIT_MARKERS):
        return "harness"
    if kind == "infra" or "patch did not apply" in reason:
        return "harness"
    if "worktree" in reason and "could not be" in reason:
        return "harness"
    if "configured check failed" in reason:
        return "code"
    if kind == "no_work":
        return "ticket"
    return None
