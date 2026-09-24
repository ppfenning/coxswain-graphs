"""Shadow-mode report: per role, how often System One agreed with the LLM node at a threshold.

Pure. Rows are the dicts `decision_log.to_row` produces; nothing is read here.

Gap upstream: `SystemOneRunner.run` in runner/system_one.py returns an on-mode fast path
or fallthrough untagged, so no producer writes on-mode rows yet. `flagged_approves` stays
empty on real logs until one does. The tests use hand-built on-mode rows.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

__all__ = ["MIN_AGREEMENT", "MIN_ROWS", "RoleReport", "report"]

MIN_AGREEMENT = 0.95
MIN_ROWS = 100
_APPROVING = frozenset({"approve", "true"})


@dataclass(frozen=True, kw_only=True)
class RoleReport:
    """One role's shadow summary.

    `shadow_calls` counts scored shadow rows only; rows with a None agreed or
    confidence are in `skipped` and nowhere else. `flagged_approves` holds the
    outcome_keys of on-mode approves (exact lowercase match) that were quarantined.
    """

    role: str
    shadow_calls: int
    at_threshold: int
    agreement_rate: float | None
    coverage: float | None
    eligible_for_on: bool
    skipped: int
    flagged_approves: tuple[str, ...]


def _scored(row: Mapping[str, Any]) -> bool:
    return row.get("system_one_agreed") is not None and row.get("system_one_confidence") is not None


def _summarise(
    role: str,
    rows: Sequence[Mapping[str, Any]],
    threshold: float,
    outcomes: Mapping[str, str],
) -> RoleReport:
    shadow = [r for r in rows if r.get("system_one_mode") == "shadow"]
    scored = [r for r in shadow if _scored(r)]
    at = [r for r in scored if r["system_one_confidence"] >= threshold]
    rate = sum(1 for r in at if r["system_one_agreed"]) / len(at) if at else None
    return RoleReport(
        role=role,
        shadow_calls=len(scored),
        at_threshold=len(at),
        agreement_rate=rate,
        coverage=len(at) / len(scored) if scored else None,
        eligible_for_on=rate is not None and rate >= MIN_AGREEMENT and len(at) >= MIN_ROWS,
        skipped=len(shadow) - len(scored),
        flagged_approves=tuple(
            r["outcome_key"]
            for r in rows
            if r.get("system_one_mode") == "on"
            and r.get("system_one_answer") in _APPROVING
            and outcomes.get(r["outcome_key"]) == "quarantined"
        ),
    )


def report(
    rows: Sequence[Mapping[str, Any]],
    outcomes: Mapping[str, str] | None,
    thresholds: Mapping[str, float],
) -> dict[str, RoleReport]:
    """One RoleReport per role in `thresholds`. Rows of a role with no threshold are ignored."""
    known = outcomes if outcomes is not None else {}
    return {
        role: _summarise(role, [r for r in rows if r.get("role") == role], threshold, known)
        for role, threshold in thresholds.items()
    }
