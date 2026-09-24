"""Pure tier resolution: which tier a call gets, and why.

This module chooses a tier only. It does not choose a model, an effort or a
budget, and it never clips anything. Those are computed above the runner.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

__all__ = ["TIERS", "Hints", "Resolution", "rank", "resolve"]

TIERS = ("cheap", "standard", "deep")


@dataclass(frozen=True)
class Hints:
    """Signals about a call. Every field is optional."""

    judgment: Literal["low", "normal", "high"] | None = None
    files_changed: int | None = None
    lines_changed: int | None = None
    attempt: int | None = None


@dataclass(frozen=True)
class Resolution:
    tier: str
    reason: str


def rank(tier: str) -> int:
    """Position in TIERS, lowest first. An unknown tier raises ValueError."""
    return TIERS.index(tier)


def resolve(
    role: str,
    hints: Hints | None,
    tier_overrides: Mapping[str, str],
    profile_defaults: Mapping[str, str],
    router_tier: str | None,
    floor: str,
) -> Resolution:
    """Override, then profile default, then router, then floor; never below floor.

    `hints` is carried for the phase 3 router and does not change the choice yet.
    """
    candidates = (
        ("override", tier_overrides.get(role)),
        ("profile_default", profile_defaults.get(role)),
        ("router", router_tier),
    )
    source, chosen = next(((s, t) for s, t in candidates if t is not None), ("floor", floor))
    return Resolution(floor, f"{source} raised to floor") if rank(chosen) < rank(floor) else Resolution(chosen, source)
