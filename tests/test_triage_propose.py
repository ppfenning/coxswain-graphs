"""triage-propose: read-only, and it never silently drops an alert."""

from __future__ import annotations

import pytest

from graphs._contract import ContractViolation
from graphs.ops import triage_propose
from runner import ScriptedRunner
from runner.decision_log import RouterDecision
from runner.tier_resolution import Hints

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


def test_both_calls_declare_role_and_hints_and_no_tier(cartridge) -> None:
    scripted = runner()
    triage_propose.run(args(cartridge), scripted)
    by_role = {call["role"]: call for call in scripted.calls}
    assert {role: call["tier"] for role, call in by_role.items()} == {"triage_classify": None, "evidence_verify": None}
    assert by_role["triage_classify"]["hints"] == Hints(judgment="low")
    assert by_role["evidence_verify"]["hints"] == Hints(judgment="high")


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


DECISION = RouterDecision(
    chosen_class="fast", model="m", effort="low", budget_usd=0.5, reasons=("cheap role",), clipped_by=()
)


class DecidedRunner(ScriptedRunner):
    """A `ScriptedRunner` that accepts `router_decision` and records it, None when the caller passed none."""

    def run(self, *, router_decision=None, **kwargs):
        try:
            return super().run(**kwargs)
        finally:
            self.calls[-1] = {**self.calls[-1], "router_decision": router_decision}


def decided_runner() -> DecidedRunner:
    return DecidedRunner({"triage_classify": CLASSIFY, "evidence_verify": VERIFY_ACTIONABLE})


def test_every_call_receives_the_decision_the_source_gives_for_its_role_and_hints(cartridge) -> None:
    asked = []

    def source(role, hints):
        asked.append((role, hints))
        return DECISION

    scripted = decided_runner()
    triage_propose.run(args(cartridge, alerts=alerts(1), decision_source=source), scripted)
    assert [c["router_decision"] for c in scripted.calls] == [DECISION, DECISION]
    assert asked == [("triage_classify", Hints(judgment="low")), ("evidence_verify", Hints(judgment="high"))]


def test_the_default_source_passes_no_decision(cartridge) -> None:
    scripted = decided_runner()
    triage_propose.run(args(cartridge, alerts=alerts(1)), scripted)
    assert [c["router_decision"] for c in scripted.calls] == [None, None]


def test_a_source_that_raises_does_not_fail_the_node(cartridge) -> None:
    def source(role, hints):
        raise RuntimeError("router down")

    scripted = decided_runner()
    triage_propose.run(args(cartridge, alerts=alerts(1), decision_source=source), scripted)
    assert [c["router_decision"] for c in scripted.calls] == [None, None]
