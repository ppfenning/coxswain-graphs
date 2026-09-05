"""Epic scoping: the threshold and the routing model come off the cartridge.

Both blocks were declared in the base cartridge and read by nothing, which is
exactly the drift the cartridge seam exists to prevent — config no code
consumes is config nobody notices going stale.
"""

from __future__ import annotations

import pytest

from graphs._contract import ContractViolation, epic_shape, landing_for
from graphs.delivery import lifecycle_propose
from runner import ScriptedRunner

THRESHOLD = {"phases_min": 2, "tickets_min": 3, "multi_repo": True}
ROUTING = {"states": {"active": "board", "planned": "board_planned", "future": "future_landing"}}


@pytest.fixture
def scoped(cartridge) -> dict:
    cartridge["epic_threshold"] = dict(THRESHOLD)
    cartridge["work_routing"] = {"states": dict(ROUTING["states"])}
    cartridge["skills"]["scope_epic"] = "acme-skills:scope-epic"
    cartridge["write_kinds"]["item_create"] = {"risk": "low", "ramp": "deferred"}
    return cartridge


# ── the threshold ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "phases,tickets,repos,expected",
    [
        (1, 1, 1, "ticket"),  # most work is not an epic
        (1, 2, 1, "parent_with_subtasks"),  # two unordered units
        (2, 1, 1, "epic"),  # genuinely multi-phase
        (1, 3, 1, "epic"),  # three-plus tickets
        (1, 1, 2, "epic"),  # coordinated across repositories
    ],
)
def test_shape_comes_off_the_threshold(cartridge, phases, tickets, repos, expected) -> None:
    cartridge["epic_threshold"] = dict(THRESHOLD)
    assert epic_shape(cartridge, phases=phases, tickets=tickets, repos=repos) == expected


def test_a_team_can_move_its_own_bar(cartridge) -> None:
    """Two teams can disagree about what an epic is and both be right."""
    cartridge["epic_threshold"] = {"phases_min": 9, "tickets_min": 9, "multi_repo": False}
    assert epic_shape(cartridge, phases=2, tickets=3, repos=2) == "parent_with_subtasks"


# ── routing ────────────────────────────────────────────────────────────────


def test_routing_reads_the_cartridge(cartridge) -> None:
    cartridge["work_routing"] = {"states": dict(ROUTING["states"])}
    assert landing_for(cartridge, "active") == "board"
    assert landing_for(cartridge, "future") == "future_landing"


def test_unscoped_work_never_lands_on_the_active_board(cartridge) -> None:
    cartridge["work_routing"] = {"states": dict(ROUTING["states"])}
    assert landing_for(cartridge, "future") != landing_for(cartridge, "active")


def test_an_unroutable_state_is_refused_rather_than_guessed(cartridge) -> None:
    cartridge["work_routing"] = {"states": dict(ROUTING["states"])}
    with pytest.raises(ContractViolation, match="routes no state 'invented'"):
        landing_for(cartridge, "invented")


# ── the node, in the graph ─────────────────────────────────────────────────


def scope_run(scoped, scope_response, plan_response, build_response, review_response):
    return lifecycle_propose.run(
        {"run_id": "r", "date": "2026-08-30", "ticket": "TICKET-1", "cartridge": scoped},
        ScriptedRunner(
            {
                "scope_epic": scope_response,
                "plan": plan_response,
                "build": build_response,
                "review_charter": review_response,
            }
        ),
    )


def test_scoping_emits_a_routed_proposal(scoped, plan_response, build_response, review_response) -> None:
    response = {
        "phases": ["p1", "p2"],
        "tickets": ["t1", "t2", "t3"],
        "repos": ["a"],
        "state": "planned",
        "parent_epic": "",
        "rationale": "multi-phase",
    }
    result = scope_run(scoped, response, plan_response, build_response, review_response)
    assert result["scope"]["shape"] == "epic"
    assert result["scope"]["landing"] == "board_planned"

    scoping = [p for p in result["proposals"] if p["kind"] == "item_create"]
    assert len(scoping) == 1
    assert "epic" in scoping[0]["suggested_action"]
    assert any(e["check"] == "epic_threshold" for e in scoping[0]["evidence"])
    assert any(e["check"] == "work_routing" for e in scoping[0]["evidence"])


def test_future_work_routes_off_the_active_board(scoped, plan_response, build_response, review_response) -> None:
    response = {
        "phases": ["p1"],
        "tickets": ["t1"],
        "repos": ["a"],
        "state": "future",
        "parent_epic": "",
        "rationale": "roadmap",
    }
    result = scope_run(scoped, response, plan_response, build_response, review_response)
    assert result["scope"]["landing"] == "future_landing"
    assert result["scope"]["shape"] == "ticket", "one unit of work is one ticket"


def test_it_attaches_to_an_existing_epic_when_one_covers_the_area(
    scoped, plan_response, build_response, review_response
) -> None:
    response = {
        "phases": ["p1"],
        "tickets": ["t1"],
        "repos": ["a"],
        "state": "planned",
        "parent_epic": "EPIC-9",
        "rationale": "same area",
    }
    result = scope_run(scoped, response, plan_response, build_response, review_response)
    scoping = next(p for p in result["proposals"] if p["kind"] == "item_create")
    assert scoping["target"] == "EPIC-9"
    assert "EPIC-9" in scoping["suggested_action"]


def test_the_scope_node_runs_before_planning(scoped, plan_response, build_response, review_response) -> None:
    """Scoping decides whether this is even one ticket; planning assumes it is."""
    scripted = ScriptedRunner(
        {
            "scope_epic": {
                "phases": ["p1"],
                "tickets": ["t1"],
                "repos": ["a"],
                "state": "planned",
                "parent_epic": "",
                "rationale": "r",
            },
            "plan": plan_response,
            "build": build_response,
            "review_charter": review_response,
        }
    )
    lifecycle_propose.run(
        {"run_id": "r", "date": "2026-08-30", "ticket": "T", "cartridge": scoped}, scripted
    )
    assert next(c["role"] for c in scripted.calls) == "scope_epic"
