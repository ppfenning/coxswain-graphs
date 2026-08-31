"""The runbook improves itself, or it rots.

A runbook is only ever amended by someone who just hit its gap, and by the time
the incident is over nobody goes back. These are the two facts a run establishes
that the runbook cannot learn any other way: a symptom nothing matched, and a
trap that turned out to be wrong.
"""

from __future__ import annotations

import pytest

from graphs import triage_propose
from graphs._contract import ContractViolation
from runner import ScriptedRunner

MATCHED = {"symptom_key": "late_landing", "runbook_entry": "rb-01", "confidence": "high"}
HEALTHY = {
    "checks": [{"check": "object listing", "output": "0 objects", "supports_symptom": True}],
    "trap_considered": "SUCCESS is not evidence the file landed",
    "trap_held": True,
    "runbook_correction": "",
    "conclusion": "feed never delivered",
    "suggested_action": "comment with the listing",
    "actionable": True,
}


@pytest.fixture
def cart(cartridge) -> dict:
    cartridge["write_kinds"]["doc_update"] = {"risk": "low", "ramp": "deferred"}
    return cartridge


def triage(cart, classify, verify, alerts=1):
    return triage_propose.run(
        {
            "run_id": "r",
            "date": "2026-08-30",
            "cartridge": cart,
            "alerts": [{"id": f"a{i}"} for i in range(alerts)],
        },
        ScriptedRunner({"triage_classify": classify, "evidence_verify": verify}),
    )


def doc_updates(result):
    return [p for p in result["proposals"] if p["kind"] == "doc_update"]


def test_a_healthy_runbook_entry_proposes_no_change(cart) -> None:
    """Self-healing that fires on every run is noise, not healing."""
    result = triage(cart, MATCHED, HEALTHY)
    assert doc_updates(result) == []
    assert result["totals"]["runbook_gaps"] == 0


def test_an_unmatched_symptom_proposes_a_new_entry(cart) -> None:
    result = triage(cart, {**MATCHED, "runbook_entry": ""}, HEALTHY)
    proposals = doc_updates(result)
    assert len(proposals) == 1
    assert "no runbook entry matched" in proposals[0]["rationale"]
    assert "with its trap" in proposals[0]["suggested_action"], "a new entry must carry its trap"


def test_a_trap_that_did_not_hold_proposes_an_amendment(cart) -> None:
    """The worst case: the runbook names a wrong belief that is itself wrong."""
    result = triage(cart, MATCHED, {**HEALTHY, "trap_held": False})
    proposals = doc_updates(result)
    assert len(proposals) == 1
    assert "trap" in proposals[0]["rationale"]
    assert "rb-01" in proposals[0]["suggested_action"]


def test_an_explicit_correction_is_carried_verbatim(cart) -> None:
    result = triage(cart, MATCHED, {**HEALTHY, "runbook_correction": "the check reads local time, not UTC"})
    proposals = doc_updates(result)
    assert "local time, not UTC" in proposals[0]["suggested_action"]


def test_a_weak_match_proposes_sharpening_rather_than_a_new_entry(cart) -> None:
    result = triage(cart, {**MATCHED, "confidence": "low"}, HEALTHY)
    proposals = doc_updates(result)
    assert len(proposals) == 1
    assert "sharpen" in proposals[0]["suggested_action"]


def test_runbook_proposals_carry_the_checks_as_evidence(cart) -> None:
    result = triage(cart, {**MATCHED, "runbook_entry": ""}, HEALTHY)
    evidence = doc_updates(result)[0]["evidence"]
    assert {"check": "object listing", "output": "0 objects"} in evidence


def test_self_healing_stays_propose_only(cart) -> None:
    """A doc_update is a proposal, so a read-only graph stays read-only."""
    result = triage(cart, {**MATCHED, "runbook_entry": ""}, HEALTHY)
    assert doc_updates(result), "it proposed something"
    assert result["totals"]["runbook_gaps"] == 1
    # `doc_update` is deferred in the taxonomy: it cannot auto-apply until the
    # eligible kinds have earned their ramp.
    assert cart["write_kinds"]["doc_update"]["ramp"] == "deferred"


def test_it_refuses_when_the_cartridge_has_no_doc_update_kind(cartridge) -> None:
    """An unknown kind refuses rather than being invented at the node."""
    cartridge["write_kinds"].pop("doc_update", None)
    with pytest.raises(ContractViolation, match="unknown write kind 'doc_update'"):
        triage(cartridge, {**MATCHED, "runbook_entry": ""}, HEALTHY)


def test_gaps_are_counted_per_alert(cart) -> None:
    result = triage(cart, {**MATCHED, "runbook_entry": ""}, HEALTHY, alerts=3)
    assert result["totals"]["runbook_gaps"] == 3
    assert len(doc_updates(result)) == 3
