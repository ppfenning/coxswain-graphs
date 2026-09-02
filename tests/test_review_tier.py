"""Tier 0 by size: small, one module, no dangerous surface — the charter reviewer alone."""

from graphs._contract import review_tier

CART = {"policy": {"review_tier": {"tier0_max_changed_lines": 30, "tier1_max_changed_lines": 150,
                                   "tier1_max_modules": 1, "tier2_surfaces": ["auth"], "tier0_patterns": ["docs_only"]}}}


def facts(lines: int, modules: int = 1) -> dict:
    return {"changed_lines": lines, "module_count": modules}


def test_a_small_contained_change_is_tier_zero() -> None:
    assert review_tier(CART, change_facts=facts(12)) == 0


def test_size_never_talks_down_a_dangerous_surface() -> None:
    assert review_tier(CART, change_facts=facts(3), surfaces=["auth"]) == 2


def test_two_modules_is_not_small_however_few_the_lines() -> None:
    assert review_tier(CART, change_facts=facts(10, modules=2)) == 2


def test_the_size_gate_is_off_unless_the_cartridge_sets_it() -> None:
    off = {"policy": {"review_tier": {**CART["policy"]["review_tier"], "tier0_max_changed_lines": 0}}}
    assert review_tier(off, change_facts=facts(5)) == 1
