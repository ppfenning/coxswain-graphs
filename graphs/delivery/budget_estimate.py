"""Estimate a build task's dollar budget from landed-task history.

A pure core with no clock, no filesystem, and no network: every fact
`estimate_budget` needs arrives in the `history` sequence, and the answer is
a plain `(amount, source)` tuple. The caller — a later task — is the one that
queries the harness store for the last 30 days of landed build tasks and
turns each row into the `(repo, surfaces_count, build_cost_usd)` shape this
module consumes; this module never touches the store itself, in the same
style as `graphs/delivery/checkpoint.py`.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

__all__ = ["estimate_budget"]

_FLOOR_USD = 1.00
_CEILING_USD = 4.00
_DEFAULT_USD = 1.50
_MIN_ROWS = 10


def _bucket(surfaces_count: int) -> str:
    """Bucket a task's surface count into one of `1`, `2`, `3-4`, `5+`."""
    if surfaces_count <= 1:
        return "1"
    if surfaces_count == 2:
        return "2"
    if surfaces_count <= 4:
        return "3-4"
    return "5+"


def _p75(values: Sequence[float]) -> float:
    """The 75th percentile of `values` by linear interpolation between ranks.

    This is the R-7 / numpy-`linear` method: the same value any spot check
    against `numpy.percentile(values, 75)` would produce.
    """
    ordered = sorted(values)
    n = len(ordered)
    if n == 1:
        return ordered[0]
    rank = 0.75 * (n - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return ordered[int(rank)]
    frac = rank - lo
    return ordered[lo] + (ordered[hi] - ordered[lo]) * frac


def _clamp_to_cents(value: float) -> float:
    return round(max(_FLOOR_USD, min(_CEILING_USD, value)), 2)


def estimate_budget(history: Sequence[tuple[str, int, float]], repo: str, surfaces_count: int) -> tuple[float, str]:
    """Estimate a build task's dollar budget from landed-task history.

    Buckets `surfaces_count` into `1`, `2`, `3-4`, or `5+` and applies three
    rules in order, first match wins:

    1. Rows matching this repo and this bucket: if there are at least 10,
       their p75.
    2. Rows matching this repo, any bucket: if there are at least 10, their
       p75.
    3. Otherwise, `1.50`.

    Every path returns source `"estimate"`; the result is clamped to
    `[1.00, 4.00]` and rounded to cents.
    """
    bucket = _bucket(surfaces_count)
    repo_rows = [cost for row_repo, _row_surfaces, cost in history if row_repo == repo]
    bucket_rows = [
        cost for row_repo, row_surfaces, cost in history if row_repo == repo and _bucket(row_surfaces) == bucket
    ]
    if len(bucket_rows) >= _MIN_ROWS:
        return _clamp_to_cents(_p75(bucket_rows)), "estimate"
    if len(repo_rows) >= _MIN_ROWS:
        return _clamp_to_cents(_p75(repo_rows)), "estimate"
    return _clamp_to_cents(_DEFAULT_USD), "estimate"
