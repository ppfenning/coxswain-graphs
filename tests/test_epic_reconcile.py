"""epic-reconcile: drift is established by set arithmetic, not asked of a model."""

from __future__ import annotations

import pytest

from graphs._contract import ContractViolation
from graphs.ops import epic_reconcile
from runner import ScriptedRunner

EPIC = {
    "id": "EPIC-1",
    "tickets": [
        {"id": "T-1", "state": "in_progress"},
        {"id": "T-2", "state": "in_progress"},
        {"id": "T-3", "state": "done"},
    ],
}
OBSERVED = {
    "tickets": [
        {"id": "T-1", "state": "in_progress"},  # agrees
        {"id": "T-2", "state": "done"},  # closed on the board, not in the epic
        {"id": "T-4", "state": "in_progress"},  # added, never attached
    ],
}


@pytest.fixture
def cart(cartridge) -> dict:
    cartridge["skills"]["reconcile"] = "acme-skills:reconcile"
    cartridge["work_routing"] = {"states": {"active": "board", "planned": "board_planned", "future": "future"}}
    cartridge["write_kinds"]["item_update"] = {"risk": "low", "ramp": "deferred"}
    cartridge["write_kinds"]["state_move"] = {"risk": "low", "ramp": "deferred"}
    return cartridge


def reconcile(cart, response, epic=EPIC, observed=OBSERVED):
    return epic_reconcile.run(
        {"run_id": "r", "date": "2026-08-30", "cartridge": cart, "epic": epic, "observed": observed},
        ScriptedRunner({"reconcile": response}),
    )


RESPONSE = {
    "drifts": [
        {"ticket": "T-2", "declared": "in_progress", "actual": "done", "correction": "item_update", "detail": "closed on the board"},
        {"ticket": "T-3", "declared": "done", "actual": "absent from the board", "correction": "none", "detail": "legitimately archived"},
        {"ticket": "T-4", "declared": "absent from the epic", "actual": "in_progress", "correction": "state_move", "detail": "never attached"},
    ],
    "summary": "two real drifts",
}


def test_divergences_are_computed_not_asked(cart) -> None:
    result = reconcile(cart, RESPONSE)
    drifted = {d["ticket"] for d in result["divergences"]}
    assert drifted == {"T-2", "T-3", "T-4"}, "T-1 agrees and must not be flagged"


def test_an_epic_that_matches_reality_runs_no_node_at_all(cart) -> None:
    """No drift, no model call. Asking a model to confirm nothing happened is how
    you get told something did."""
    result = epic_reconcile.run(
        {"run_id": "r", "date": "d", "cartridge": cart, "epic": EPIC, "observed": {"tickets": EPIC["tickets"]}},
        ScriptedRunner({}),  # would raise if the reconcile node were called
    )
    assert result["proposals"] == []
    assert result["totals"]["drifted"] == 0


def test_corrections_become_proposals_of_the_named_kind(cart) -> None:
    result = reconcile(cart, RESPONSE)
    kinds = sorted(p["kind"] for p in result["proposals"])
    assert kinds == ["item_update", "state_move"]


def test_a_legitimate_difference_proposes_nothing(cart) -> None:
    result = reconcile(cart, RESPONSE)
    assert not [p for p in result["proposals"] if p["target"] == "T-3"]


def test_a_correction_for_a_ticket_that_never_drifted_is_refused(cart) -> None:
    """The node does not get to invent a drift the set comparison never found."""
    invented = {
        "drifts": [{"ticket": "T-1", "declared": "x", "actual": "y", "correction": "item_update", "detail": "made up"}],
        "summary": "s",
    }
    result = reconcile(cart, invented)
    assert result["proposals"] == []


def test_proposals_carry_both_sides_as_evidence(cart) -> None:
    result = reconcile(cart, RESPONSE)
    proposal = next(p for p in result["proposals"] if p["target"] == "T-2")
    outputs = {e["output"] for e in proposal["evidence"]}
    assert "in_progress" in outputs and "done" in outputs


def test_board_moves_cite_the_routing_model(cart) -> None:
    result = reconcile(cart, RESPONSE)
    proposal = next(p for p in result["proposals"] if p["kind"] == "state_move")
    assert any(e["check"] == "work_routing" for e in proposal["evidence"])


def test_it_refuses_to_fetch_the_board_itself(cart) -> None:
    with pytest.raises(ContractViolation, match="args.observed is required"):
        epic_reconcile.run(
            {"run_id": "r", "date": "d", "cartridge": cart, "epic": EPIC}, ScriptedRunner({})
        )


def test_it_refuses_without_a_cartridge(cart) -> None:
    with pytest.raises(ContractViolation, match="cartridge"):
        epic_reconcile.run({"run_id": "r", "date": "d", "epic": EPIC, "observed": OBSERVED}, ScriptedRunner({}))


def test_a_team_without_the_reconcile_role_is_told_so(cartridge) -> None:
    cartridge["work_routing"] = {"states": {"active": "board"}}
    with pytest.raises(ContractViolation, match="needs the optional role 'reconcile'"):
        epic_reconcile.run(
            {"run_id": "r", "date": "d", "cartridge": cartridge, "epic": EPIC, "observed": OBSERVED},
            ScriptedRunner({}),
        )
