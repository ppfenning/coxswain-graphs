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

# An expired or missing credential means the node never ran. Lowercase, matched
# against the lowercased reason.
_AUTH_MARKERS: tuple[str, ...] = (
    "failed to authenticate",
    "oauth session expired",
    "not logged in",
    "invalid api key",
    "authentication_error",
)

# Not an auth stop: concurrent Claude Code processes collided on the OAuth refresh. The
# credential is fine and a retry works. Keep it out of `_AUTH_MARKERS`, which stop the
# whole run in harness/epic.py. Lowercase, matched against the lowercased reason.
_TRANSIENT_AUTH: tuple[str, ...] = ("failed to refresh oauth token",)


# Must equal `harness.checks.HARNESS_FAULT_PREFIX`; this module imports nothing from the harness.
_HARNESS_FAULT_PREFIX = "harness fault:"


def is_auth_failure(reason: str) -> bool:
    """True when the reason carries an authentication marker; `classify_cause` asks the same."""
    return any(marker in reason.lower() for marker in _AUTH_MARKERS)


def classify_cause(kind: str, reason: str) -> Cause | None:
    """An authentication failure first, then a budget, turn or session limit stop, then a transient OAuth refresh collision, then harness, code, ticket; None when no rule matches."""
    if is_auth_failure(reason):
        return "harness"
    if any(marker in reason.lower() for marker in _LIMIT_MARKERS):
        return "harness"
    if any(marker in reason.lower() for marker in _TRANSIENT_AUTH):
        return "harness"
    if kind == "infra" or "patch did not apply" in reason:
        return "harness"
    if "worktree" in reason and "could not be" in reason:
        return "harness"
    if reason.startswith(_HARNESS_FAULT_PREFIX):
        return "harness"
    if "configured check failed" in reason or "configured checks failed" in reason:
        return "code"
    if kind == "no_work":
        return "ticket"
    return None
