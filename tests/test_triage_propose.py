"""triage-propose: read-only, and it never silently drops an alert."""

from __future__ import annotations

import pytest

from graphs._contract import ContractViolation
from graphs.ops import triage_propose
from runner import ScriptedRunner

CLASSIFY = {"symptom_key": "late_landing", "runbook_entry": "rb-01", "confidence": "high"}
VERIFY_ACTIONABLE = {
    "checks": [{"check": "list objects at prefix", "output": "0 objects", "supports_symptom": True}],
    "trap_considered": "job status SUCCESS does not mean the file landed",
    "conclusion": "the upstream feed never delivered",
    "suggested_action": "comment on the alert with the object listing",
    "actionable": True,
}


def alerts(n: int):
    return [{"id": f"alert-{i}", "text": "pipeline reported success"} for i in range(n)]


def args(cartridge, **overrides):
    return {"run_id": "run-1", "date": "2026-08-30", "cartridge": cartridge, "alerts": alerts(3), **overrides}


def runner(classify=CLASSIFY, verify=VERIFY_ACTIONABLE):
    return ScriptedRunner({"triage_classify": classify, "evidence_verify": verify})


def test_runs_end_to_end_and_emits_proposals_with_evidence(cartridge) -> None:
    result = triage_propose.run(args(cartridge), runner())
    assert len(result["proposals"]) == 3
    assert result["proposals"][0]["kind"] == "comment_add"
    assert result["proposals"][0]["evidence"] == [{"check": "list objects at prefix", "output": "0 objects"}]


def test_classify_is_cheap_and_verify_is_deep(cartridge) -> None:
    scripted = runner()
    triage_propose.run(args(cartridge), scripted)
    tiers = {call["role"]: call["tier"] for call in scripted.calls}
    assert tiers == {"triage_classify": "cheap", "evidence_verify": "deep"}


def test_overflow_is_counted_and_deferred_never_dropped(cartridge) -> None:
    """A graph that drops nine of ten alerts and reports success is worse than one that fails."""
    result = triage_propose.run(args(cartridge, alerts=alerts(20), max_alerts=6, verify_cap=2), runner())
    totals = result["totals"]
    assert totals["received"] == 20
    assert totals["fetched"] == 6
    assert totals["deferred_overflow"] == 14
    assert totals["deferred_for_capacity"] == 4
    assert totals["verified"] == 2


def test_unverified_alerts_are_still_reported_not_discarded(cartridge) -> None:
    result = triage_propose.run(args(cartridge, alerts=alerts(5), max_alerts=5, verify_cap=2), runner())
    assert len(result["triaged"]) == 5
    assert [t["verified"] for t in result["triaged"]] == [True, True, False, False, False]


def test_only_verified_alerts_can_produce_a_proposal(cartridge) -> None:
    result = triage_propose.run(args(cartridge, alerts=alerts(5), max_alerts=5, verify_cap=2), runner())
    assert len(result["proposals"]) == 2, "an unverified alert has no evidence, so it cannot propose"


def test_a_non_actionable_verification_proposes_nothing(cartridge) -> None:
    quiet = {**VERIFY_ACTIONABLE, "actionable": False}
    result = triage_propose.run(args(cartridge), runner(verify=quiet))
    assert result["proposals"] == []


def test_refuses_a_verify_cap_larger_than_the_fetch_cap(cartridge) -> None:
    with pytest.raises(ContractViolation, match="exceeds max_alerts"):
        triage_propose.run(args(cartridge, max_alerts=3, verify_cap=10), runner())


def test_refuses_to_fetch_the_queue_itself(cartridge) -> None:
    """Alerts arrive as an argument; a node that fetches cannot be replayed."""
    incomplete = {"run_id": "r", "date": "2026-08-30", "cartridge": cartridge}
    with pytest.raises(ContractViolation, match="args.alerts is required"):
        triage_propose.run(incomplete, runner())


def test_runbook_index_comes_off_the_cartridge(cartridge) -> None:
    cartridge["landing_areas"]["runbook_index"] = "/fake/acme/runbooks/index.md"
    scripted = runner()
    triage_propose.run(args(cartridge), scripted)
    assert "/fake/acme/runbooks/index.md" in scripted.calls[0]["context"]


def test_writes_nothing_anywhere(cartridge, tmp_path) -> None:
    """The whole graph is read-only. Nothing it does should touch the disk."""
    before = set(tmp_path.rglob("*"))
    triage_propose.run(args(cartridge), runner())
    assert set(tmp_path.rglob("*")) == before
