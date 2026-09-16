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

from graphs.ops.triage_quarantine import check_citation, facts

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
