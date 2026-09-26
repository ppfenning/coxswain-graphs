"""Scope files an item only for a ticket that is not already one."""

from __future__ import annotations

import pytest

from graphs.delivery import lifecycle_propose
from runner import ScriptedRunner

SCOPE = {
    "phases": ["p1"],
    "tickets": ["t1"],
    "repos": ["a"],
    "state": "planned",
    "parent_epic": "",
    "rationale": "one unit",
}


@pytest.fixture
def scoped(cartridge) -> dict:
    cartridge["epic_threshold"] = {"phases_min": 2, "tickets_min": 3, "multi_repo": True}
    cartridge["work_routing"] = {"states": {"active": "board", "planned": "board_planned", "future": "future_landing"}}
    cartridge["skills"]["scope_epic"] = "acme-skills:scope-epic"
    cartridge["write_kinds"]["item_create"] = {"risk": "low", "ramp": "deferred"}
    return cartridge


def run(scoped, plan_response, build_response, review_response, **extra):
    return lifecycle_propose.run(
        {"run_id": "r", "date": "2026-09-25", "ticket": "TICKET-1", "cartridge": scoped, **extra},
        ScriptedRunner(
            {"scope_epic": SCOPE, "plan": plan_response, "build": build_response, "review_charter": review_response}
        ),
    )


def test_a_run_marked_as_a_work_item_proposes_no_item_create(
    scoped, plan_response, build_response, review_response
) -> None:
    result = run(scoped, plan_response, build_response, review_response, work_item=True)
    assert [p for p in result["proposals"] if p["kind"] == "item_create"] == []
    assert result["scope"]["shape"] == "ticket"
    assert result["scope"]["landing"] == "board_planned"


def test_an_unmarked_run_still_proposes_one_item_create(
    scoped, plan_response, build_response, review_response
) -> None:
    result = run(scoped, plan_response, build_response, review_response)
    assert len([p for p in result["proposals"] if p["kind"] == "item_create"]) == 1
