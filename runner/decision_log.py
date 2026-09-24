"""The record of one model-routing decision, shared by every runner and decider.

Deliberately generic: nothing here belongs to one runner. The one runner-specific
field is `claude_code_version`, which stays None for any other runner. Values are
not validated; the router that fills the record owns the rules.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

__all__ = ["CallDecision", "from_row", "to_row"]


@dataclass(frozen=True, kw_only=True)
class CallDecision:
    """What was asked for, what was chosen, and which bound clipped it.

    The decision is a triple: model, effort, budget_usd. `clipped_by` names the
    chair bound that reduced it, or None when nothing did. `system_one_agreed` is
    set only in shadow mode: whether the prediction matched the LLM node's answer.
    """

    role: str
    requested_tier: str
    chosen_tier: str
    model_id: str
    reason: str
    ticket_key: str
    outcome_key: str
    router_tier: str | None = None
    router_reason: str | None = None
    claude_code_version: str | None = None
    effort: str | None = None
    budget_usd: float | None = None
    clipped_by: str | None = None
    system_one_backend: str | None = None
    system_one_mode: str | None = None
    system_one_answer: str | None = None
    system_one_confidence: float | None = None
    system_one_threshold: float | None = None
    system_one_agreed: bool | None = None


def to_row(decision: CallDecision) -> dict[str, Any]:
    """Every field, unset optionals kept as None."""
    return asdict(decision)


def from_row(row: Mapping[str, Any]) -> CallDecision:
    """Inverse of `to_row`. Absent optional keys load as None; a missing required key raises KeyError."""
    return CallDecision(
        role=row["role"],
        requested_tier=row["requested_tier"],
        chosen_tier=row["chosen_tier"],
        model_id=row["model_id"],
        reason=row["reason"],
        ticket_key=row["ticket_key"],
        outcome_key=row["outcome_key"],
        router_tier=row.get("router_tier"),
        router_reason=row.get("router_reason"),
        claude_code_version=row.get("claude_code_version"),
        effort=row.get("effort"),
        budget_usd=row.get("budget_usd"),
        clipped_by=row.get("clipped_by"),
        system_one_backend=row.get("system_one_backend"),
        system_one_mode=row.get("system_one_mode"),
        system_one_answer=row.get("system_one_answer"),
        system_one_confidence=row.get("system_one_confidence"),
        system_one_threshold=row.get("system_one_threshold"),
        system_one_agreed=row.get("system_one_agreed"),
    )
