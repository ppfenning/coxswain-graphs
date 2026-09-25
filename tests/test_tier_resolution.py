import dataclasses

import pytest

from runner.tier_resolution import CLASSES, TIER_TO_CLASS, TIERS, Hints, Resolution, rank, resolve, to_tier


def _resolve(overrides=None, defaults=None, router=None, floor="cheap", role="plan"):
    return resolve(role, None, overrides or {}, defaults or {}, router, floor)


def test_an_override_beats_the_profile_default_the_router_and_the_floor():
    got = _resolve({"plan": "deep"}, {"plan": "standard"}, "standard", "cheap")
    assert got == Resolution("deep", "override")


def test_the_profile_default_beats_the_router_and_the_floor():
    got = _resolve({}, {"plan": "standard"}, "deep", "cheap")
    assert got == Resolution("standard", "profile_default")


def test_the_router_beats_the_floor():
    assert _resolve(router="standard") == Resolution("standard", "router")


def test_the_floor_is_the_last_resort():
    assert _resolve(floor="standard") == Resolution("standard", "floor")


def test_a_router_of_none_is_skipped():
    got = _resolve(defaults={"plan": "deep"}, router=None)
    assert got == Resolution("deep", "profile_default")


def test_an_override_for_another_role_is_ignored():
    got = _resolve({"build": "deep"}, {"plan": "standard"})
    assert got == Resolution("standard", "profile_default")


def test_a_choice_below_the_floor_is_raised_to_it():
    got = _resolve({"plan": "cheap"}, floor="standard")
    assert got == Resolution("standard", "override raised to floor")


def test_hints_default_to_none_and_are_frozen():
    hints = Hints()
    assert (hints.judgment, hints.files_changed, hints.lines_changed, hints.attempt) == (None,) * 4
    with pytest.raises(dataclasses.FrozenInstanceError):
        hints.attempt = 2


def test_rank_orders_the_tiers_and_refuses_an_unknown_one():
    assert [rank(t) for t in ("cheap", "standard", "deep")] == [0, 1, 2]
    with pytest.raises(ValueError):
        rank("huge")


def test_both_vocabularies_share_one_rank_order_and_frontier_reads_as_deep():
    assert dict(TIER_TO_CLASS) == {"cheap": "extract", "standard": "reason", "deep": "judge"}
    names = ("cheap", "extract", "standard", "reason", "deep", "judge", "frontier")
    assert [rank(n) for n in names] == [0, 0, 1, 1, 2, 2, 3]
    assert to_tier("frontier") == "deep"


def test_a_rank_clamped_to_the_legacy_top_indexes_the_same_tier_as_the_name_does():
    names = TIERS + CLASSES
    assert [TIERS[min(rank(n), len(TIERS) - 1)] for n in names] == [to_tier(n) for n in names]


def test_a_two_argument_resolution_takes_the_class_of_its_tier_and_equality_sees_it():
    assert Resolution("deep", "caller").chosen_class == "judge"
    assert Resolution("deep", "caller") == Resolution("deep", "caller", "judge")
    assert Resolution("deep", "caller") != Resolution("deep", "caller", "frontier")


def test_a_legacy_tier_and_a_class_name_resolve_to_the_same_class():
    assert _resolve({"plan": "deep"}) == _resolve({"plan": "judge"}) == Resolution("deep", "override", "judge")


def test_a_class_named_profile_default_beats_the_floor():
    assert _resolve(defaults={"plan": "judge"}, floor="cheap") == Resolution("deep", "profile_default", "judge")


def test_an_override_beats_a_profile_default_in_the_class_vocabulary():
    got = _resolve({"plan": "reason"}, {"plan": "judge"}, floor="extract")
    assert got == Resolution("standard", "override", "reason")


def test_an_unknown_name_falls_to_the_floor_with_a_reason():
    got = _resolve({"plan": "huge"}, floor="standard")
    assert got == Resolution("standard", "floor (unknown 'huge' from override)", "reason")


def test_an_unknown_name_is_skipped_for_the_next_valid_candidate_and_named_in_the_reason():
    got = _resolve({"plan": "huge"}, {"plan": "judge"}, floor="cheap")
    assert got == Resolution("deep", "profile_default (unknown 'huge' from override)", "judge")


def test_a_class_named_choice_below_a_class_named_floor_is_raised_to_it():
    got = _resolve({"plan": "extract"}, floor="judge")
    assert got == Resolution("deep", "override raised to floor", "judge")
