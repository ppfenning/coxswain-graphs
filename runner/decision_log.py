"""The record of one model-routing decision, shared by every runner and decider.

Deliberately generic: nothing here belongs to one runner. The one runner-specific
field is `claude_code_version`, which stays None for any other runner. CallDecision
values are not validated; the router that fills the record owns the rules.
`from_wire` checks the wire dict's shape and schema number only.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

__all__ = [
    "REASON_SEPARATOR",
    "WIRE_SCHEMA",
    "CallDecision",
    "RouterDecision",
    "from_row",
    "from_wire",
    "joined_reasons",
    "to_row",
]

WIRE_SCHEMA = 1
REASON_SEPARATOR = "; "


@dataclass(frozen=True, kw_only=True)
class RouterDecision:
    """The caller's shadow decision, as the wire dict carries it."""

    chosen_class: str
    model: str
    effort: str
    budget_usd: float
    reasons: tuple[str, ...]
    clipped_by: tuple[str, ...]


def _strings(value: Any) -> tuple[str, ...] | None:
    """A list or tuple of str as a tuple; None for anything else."""
    if isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
        return tuple(value)
    return None


def _finite_number(value: Any) -> bool:
    """An int or float that is not a bool, NaN or infinity."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def from_wire(wire: Any) -> RouterDecision | None:
    """Decode a schema-1 wire dict; reasons and clipped_by are lists of str. None on a bad shape or schema."""
    if not isinstance(wire, Mapping):
        return None
    schema = wire.get("schema")
    reasons = _strings(wire.get("reasons"))
    clipped_by = _strings(wire.get("clipped_by"))
    budget = wire.get("budget_usd")
    ok = (
        type(schema) is int
        and schema == WIRE_SCHEMA
        and reasons is not None
        and clipped_by is not None
        and all(isinstance(wire.get(k), str) for k in ("chosen_class", "model", "effort"))
        and _finite_number(budget)
    )
    if ok:
        return RouterDecision(
            chosen_class=wire["chosen_class"],
            model=wire["model"],
            effort=wire["effort"],
            budget_usd=float(budget),
            reasons=reasons,
            clipped_by=clipped_by,
        )
    return None


def joined_reasons(decision: RouterDecision) -> str:
    """The value a CallDecision's `router_reason` holds: the reasons joined by REASON_SEPARATOR."""
    return REASON_SEPARATOR.join(decision.reasons)


@dataclass(frozen=True, kw_only=True)
class CallDecision:
    """What was asked for, what was chosen, and which bound clipped it.

    The decision is a triple: model, effort, budget_usd. `clipped_by` names the
    chair bound that reduced it, or None when nothing did. `system_one_agreed` is
    set only in shadow mode: whether the prediction matched the LLM node's answer.
    The `router_*` fields carry the caller's shadow RouterDecision: `router_tier` is
    its chosen_class and `router_reason` is `joined_reasons` of it.
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
    router_model: str | None = None
    router_effort: str | None = None
    router_budget_usd: float | None = None
    router_clipped_by: tuple[str, ...] | None = None
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
    """Inverse of `to_row`. Absent optional keys load as None.

    A missing required key raises KeyError; a router_clipped_by that is not a list of str raises ValueError.
    """
    raw_clipped = row.get("router_clipped_by")
    clipped = None if raw_clipped is None else _strings(raw_clipped)
    if raw_clipped is not None and clipped is None:
        raise ValueError(f"router_clipped_by is not a list of str: {raw_clipped!r}")
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
        router_model=row.get("router_model"),
        router_effort=row.get("router_effort"),
        router_budget_usd=row.get("router_budget_usd"),
        router_clipped_by=clipped,
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
