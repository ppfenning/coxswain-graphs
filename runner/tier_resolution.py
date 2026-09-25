"""Pure tier resolution: which tier a call gets, and why.

Accepts either vocabulary: legacy tiers (cheap, standard, deep) and capability
classes (extract, reason, judge, frontier). This module chooses a tier and its
class only. It does not choose a model, an effort or a budget, and it never
clips anything. Those are computed above the runner.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

__all__ = ["CLASSES", "TIERS", "TIER_TO_CLASS", "Hints", "Resolution", "rank", "resolve", "to_class", "to_tier"]

TIERS = ("cheap", "standard", "deep")
CLASSES = ("extract", "reason", "judge", "frontier")
TIER_TO_CLASS = MappingProxyType({"cheap": "extract", "standard": "reason", "deep": "judge"})
# The legacy vocabulary has no tier above deep, so frontier reads as deep there.
_CLASS_TO_TIER = MappingProxyType({"extract": "cheap", "reason": "standard", "judge": "deep", "frontier": "deep"})


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
    chosen_class: str | None = None

    def __post_init__(self) -> None:
        """A two-argument Resolution, as the runners still build, takes the class of its tier."""
        # Frozen-dataclass idiom: the one write, at construction, fills a default derived from `tier`.
        object.__setattr__(self, "chosen_class", self.chosen_class if self.chosen_class is not None else to_class(self.tier))


def to_class(name: str) -> str | None:
    """Capability class for a legacy tier or a class name; None when unknown."""
    return TIER_TO_CLASS.get(name, name if name in CLASSES else None)


def to_tier(name: str) -> str:
    """Legacy tier for a tier or a class name. An unknown name raises ValueError."""
    if name in TIERS:
        return name
    if name in _CLASS_TO_TIER:
        return _CLASS_TO_TIER[name]
    raise ValueError(f"unknown tier or class: {name!r}")


def rank(tier: str) -> int:
    """Position in CLASSES, lowest first, for a tier or a class. Unknown raises ValueError."""
    cls = to_class(tier)
    if cls is None:
        raise ValueError(f"unknown tier or class: {tier!r}")
    return CLASSES.index(cls)


def resolve(
    role: str,
    hints: Hints | None,
    tier_overrides: Mapping[str, str],
    profile_defaults: Mapping[str, str],
    router_tier: str | None,
    floor: str,
) -> Resolution:
    """Override, then profile default, then router, then floor; never below floor.

    An unknown name is skipped and named in the reason. `hints` is carried for
    the phase 3 router and does not change the choice yet.
    """
    given = tuple(
        (s, n)
        for s, n in (
            ("override", tier_overrides.get(role)),
            ("profile_default", profile_defaults.get(role)),
            ("router", router_tier),
        )
        if n is not None
    )
    first = next((i for i, (_, n) in enumerate(given) if to_class(n) is not None), len(given))
    note = "".join(f" (unknown {n!r} from {s})" for s, n in given[:first])
    source, chosen = given[first] if first < len(given) else ("floor", floor)
    below = rank(chosen) < rank(floor)
    kept = floor if below else chosen
    reason = f"{source} raised to floor{note}" if below else f"{source}{note}"
    return Resolution(to_tier(kept), reason, to_class(kept))
