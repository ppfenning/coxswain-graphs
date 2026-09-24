import dataclasses

import pytest

from runner.tier_resolution import Hints, Resolution, rank, resolve


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
