# docs/design/triage.md §1, quoted:
#
#     `facts` (no model): from the work item and the task records of its
#     attempts — quarantine reason and `kind` per attempt, review / adversary
#     / arbitration verdicts, `fix_loop.stopped`, the trace's final result
#     `subtype` per node, the evidence array, and whether the attempts
#     predate the ticket's last revision (`attempts[].ts` vs the item file's
#     last commit). Keyed facts: `attempt-<n>|<field>`.
#
#     A cite naming a fact key not in `facts` is a fabricated citation and
#     the answer is refused; zero cites is refused; a `diagnosis` that
#     quotes no objection text from the record is refused.
#
# Chair correction 2026-09-16: subtype arrives on an attempt record only via
# an optional `node_subtypes: {node: subtype}` mapping the caller assembles;
# when absent, `facts` emits no `subtype:` key for that attempt.

import pytest

from graphs.ops import triage_quarantine
from graphs.ops.triage_quarantine import check_citation, facts
from runner import ScriptedRunner

_WORK_ITEM = {"id": "t1", "title": "example", "last_commit": "2026-09-05T00:00:00Z"}

# review/adversary/arbitration below are shaped like a saved lifecycle result:
# a mapping with its own `verdict`, not a bare string.
_ATTEMPT_1 = {
    "reason": "attempt cap",
    "kind": "build",
    "review": {"verdict": "revise", "findings": [{"detail": "missing null guard", "file": "a.py"}], "rationale": "one required check failed"},
    "adversary": {"verdict": "revise", "objections": [{"claim": "x", "why_wrong": "y"}], "strongest_objection": "x"},
    "arbitration": None,
    "fix_loop": {"stopped": "budget"},
    "evidence": ["pytest -q: 1 failed"],
    "ts": "2026-09-01T00:00:00Z",
    "node_subtypes": {"build": "attempts_exhausted", "review": "revise"},
}

_ATTEMPT_2 = {
    "reason": "attempt cap",
    "kind": "build",
    "review": {"verdict": "approve", "findings": [], "rationale": "checks pass"},
    "adversary": {"verdict": "revise", "objections": [{"claim": "x", "why_wrong": "y"}], "strongest_objection": "x"},
    "arbitration": {"verdict": "revise", "sided_with": "adversary", "reasoning": "the objection holds"},
    "fix_loop": {"stopped": "attempts"},
    "evidence": [],
    "ts": "2026-09-06T00:00:00Z",
}


def test_facts_keys_every_attempts_reason_kind_verdicts_stopped_and_subtype():
    result = facts(_WORK_ITEM, [_ATTEMPT_1, _ATTEMPT_2])
    assert result == {
        "attempt-1|reason": "attempt cap",
        "attempt-1|kind": "build",
        "attempt-1|review": "revise",
        "attempt-1|adversary": "revise",
        "attempt-1|arbitration": None,
        "attempt-1|stopped": "budget",
        "attempt-1|evidence": ["pytest -q: 1 failed"],
        "attempt-1|stale": True,
        "attempt-1|subtype:build": "attempts_exhausted",
        "attempt-1|subtype:review": "revise",
        "attempt-2|reason": "attempt cap",
        "attempt-2|kind": "build",
        "attempt-2|review": "approve",
        "attempt-2|adversary": "revise",
        "attempt-2|arbitration": "revise",
        "attempt-2|stopped": "attempts",
        "attempt-2|evidence": [],
        "attempt-2|stale": False,
    }
    assert not any(key.startswith("attempt-2|subtype:") for key in result)


def test_a_cite_naming_an_objection_absent_from_the_record_is_refused_as_fabricated():
    fact_map = facts(_WORK_ITEM, [_ATTEMPT_1])
    reason = check_citation(
        ["attempt-1|reason", "attempt-9|kind"],
        "quotes attempt cap",
        fact_map,
        ["attempt cap"],
    )
    assert reason is not None
    assert "attempt-9|kind" in reason


def test_zero_cites_is_refused():
    fact_map = facts(_WORK_ITEM, [_ATTEMPT_1])
    reason = check_citation([], "quotes attempt cap", fact_map, ["attempt cap"])
    assert reason is not None
    assert "zero cites" in reason


def test_a_diagnosis_quoting_no_objection_text_is_refused():
    fact_map = facts(_WORK_ITEM, [_ATTEMPT_1])
    reason = check_citation(
        ["attempt-1|reason"],
        "the build failed for unrelated reasons",
        fact_map,
        ["attempt cap"],
    )
    assert reason is not None
    assert "quotes no objection text" in reason


# docs/design/triage.md §1's `triage`/`emit` and §6 rule 3, one literal test each.


@pytest.fixture
def cart(cartridge) -> dict:
    cartridge["write_kinds"]["ticket_amend"] = {"risk": "low", "ramp": "eligible"}
    cartridge["write_kinds"]["item_create"] = {"risk": "medium", "ramp": "gated"}
    cartridge["write_kinds"]["notify"] = {"risk": "low", "ramp": "gated"}
    return cartridge


def _args(cart, attempts, **overrides) -> dict:
    return {
        "run_id": "r0",
        "date": "2026-09-16",
        "cartridge": cart,
        "work_item": _WORK_ITEM,
        "attempts": attempts,
        **overrides,
    }


def _response(class_: str, diagnosis: str, cites: list, action: str) -> dict:
    return {"class": class_, "diagnosis": diagnosis, "cites": cites, "action": action}


def _runner(response: dict) -> ScriptedRunner:
    return ScriptedRunner({"triage": response})


def test_ticket_defect_emits_a_ticket_amend_proposal(cart):
    response = _response(
        "ticket_defect", "quotes missing null guard verbatim", ["attempt-1|review"], "append the guard clause"
    )
    result = triage_quarantine.run(_args(cart, [_ATTEMPT_1]), _runner(response))
    assert result["emit"]["kind"] == "ticket_amend"
    assert result["emit"]["rationale"] == response["diagnosis"]
    assert result["emit"]["suggested_action"] == response["action"]


def test_platform_defect_emits_an_item_create_proposal(cart):
    response = _response(
        "platform_defect",
        "missing null guard shows a platform gap, not a ticket defect",
        ["attempt-1|review"],
        "file a mechanism for null-guard lint",
    )
    result = triage_quarantine.run(_args(cart, [_ATTEMPT_1]), _runner(response))
    assert result["emit"]["kind"] == "item_create"
    assert result["emit"]["subject_new"] is True


def test_shape_emits_a_decompose_handoff(cart):
    response = _response(
        "shape",
        "missing null guard shows the ticket was really two tasks",
        ["attempt-1|review"],
        "split into a guard task and a rename task",
    )
    result = triage_quarantine.run(_args(cart, [_ATTEMPT_1]), _runner(response))
    assert result["emit"] == {
        "handoff": "decompose",
        "target": "t1",
        "evidence": [{"check": "attempt-1|review", "output": "revise"}],
        "rationale": response["diagnosis"],
        "suggested_action": response["action"],
    }


def test_genuine_reject_emits_a_notify(cart):
    response = _response(
        "genuine_reject",
        "missing null guard was a correct objection; the build was simply wrong",
        ["attempt-1|review"],
        "notify the owner and leave the item ready",
    )
    result = triage_quarantine.run(_args(cart, [_ATTEMPT_1]), _runner(response))
    assert result["emit"]["kind"] == "notify"
    assert result["escalated"] is False


def test_the_attempts_triage_entry_carries_class_diagnosis_and_run(cart):
    response = _response(
        "ticket_defect", "quotes missing null guard verbatim", ["attempt-1|review"], "append the guard clause"
    )
    result = triage_quarantine.run(_args(cart, [_ATTEMPT_1]), _runner(response))
    assert result["attempts_triage_entry"] == {
        "class": "ticket_defect",
        "diagnosis": response["diagnosis"],
        "run": "r0",
    }


def test_the_same_class_diagnosis_pair_twice_escalates_instead_of_emitting(cart):
    prior_triage = {"class": "ticket_defect", "diagnosis": "quotes missing null guard verbatim", "run": "r-9"}
    prior_attempt = {**_ATTEMPT_1, "triage": prior_triage}
    response = _response(
        "ticket_defect", "quotes missing null guard verbatim", ["attempt-1|review"], "append the guard clause again"
    )
    result = triage_quarantine.run(_args(cart, [prior_attempt]), _runner(response))
    assert result["escalated"] is True
    assert result["prior"] == prior_triage
    assert "emit" not in result
