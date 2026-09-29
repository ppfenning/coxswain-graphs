"""Pure pricing and budget-cap helpers for API runners.

Prices are plain dicts of ``{model: {"input": usd_per_million, "output": usd_per_million}}``.
Neither function reads a client, a profile object, a store handle, or a clock.
"""

from __future__ import annotations

import math

__all__ = ["max_output_tokens_for_budget", "price_call"]

_PER_MILLION = 1_000_000


def price_call(prices: dict, model: str, input_tokens: int, output_tokens: int) -> float | None:
    """Cost in USD of a call, or None when ``model`` has no entry in ``prices``."""
    if model not in prices:
        return None
    rate = prices[model]
    return input_tokens / _PER_MILLION * rate["input"] + output_tokens / _PER_MILLION * rate["output"]


def max_output_tokens_for_budget(prices: dict, model: str, budget_left_usd: float) -> int | None:
    """Output tokens the budget affords; None when ``model`` is unpriced or its output price is 0, so no cap applies."""
    if model not in prices or prices[model]["output"] == 0:
        return None
    return math.floor(budget_left_usd / (prices[model]["output"] / _PER_MILLION))
