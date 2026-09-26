"""lifecycle-propose records one summary per handoff or review round, in order."""

from __future__ import annotations

from graphs.delivery import lifecycle_propose
from graphs.delivery.lifecycle_propose import round_summary
from runner import ScriptedRunner

REVISE = {
    "verdict": "revise",
    "findings": [{"charter_principle": "A3", "detail": "mutates its argument", "file": "src/a.py"}],
    "rationale": "the argument is mutated",
}
APPROVE = {"verdict": "approve", "findings": [], "rationale": "matches the charter"}


def test_a_handoff_critique_summarises_as_a_handoff_round_with_no_objections() -> None:
    review, adversary, arbitration, verdict, _, _ = lifecycle_propose._handoff_critique(
        {"complete": False, "missing": ["a pytest run"], "brief": "supply it"}
    )
    assert round_summary(1, "handoff", review, adversary, arbitration, verdict) == {
        "attempt": 1,
        "source": "handoff",
        "verdict": "revise",
        "findings": ["handoff evidence"],
        "objections": 0,
        "arbitration": None,
    }


def test_a_review_round_with_two_objections_and_an_arbitration_summarises_to_the_literal_dict() -> None:
    adversary = {"verdict": "revise", "objections": [{"claim": "one"}, {"claim": "two"}], "strongest_objection": "one"}
    arbitration = {"verdict": "revise", "sided_with": "adversary", "reasoning": "the objection holds"}
    assert round_summary(2, "review", REVISE, adversary, arbitration, "revise") == {
        "attempt": 2,
        "source": "review",
        "verdict": "revise",
        "findings": ["A3"],
        "objections": 2,
        "arbitration": "revise",
    }


def test_a_run_revised_once_then_approved_records_two_rounds_in_order(
    cartridge, plan_response, build_response
) -> None:
    second = {**build_response, "patch": "--- a/src/a.py\n+++ b/src/a.py\n-old line\n+a different fix\n+with its own test\n"}
    scripted = ScriptedRunner(
        {"plan": plan_response, "build": [build_response, second], "review_charter": [REVISE, APPROVE]}
    )
    result = lifecycle_propose.run(
        {"run_id": "run-1", "date": "2026-09-26", "ticket": "TICKET-1", "cartridge": cartridge}, scripted
    )
    assert [(r["attempt"], r["source"], r["verdict"]) for r in result["fix_loop"]["rounds"]] == [
        (1, "review", "revise"),
        (2, "review", "approve"),
    ]
