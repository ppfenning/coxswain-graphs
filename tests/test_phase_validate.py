"""The two validators, and the independence that makes the second one worth having.

`validate_phase` exists to catch five individually-green tasks that do not add
up. It can only do that if it is not reading the builder's own account of the
work — so the property under test is not merely that the graph runs, it is that
a planted summary NEVER reaches a prompt.
"""

from __future__ import annotations

import pytest

from graphs._contract import ContractViolation
from graphs.delivery import phase_validate
from runner import ScriptedRunner

CHUNK_OK = {"satisfied": True, "gaps": [], "reasoning": "the description is satisfied"}
CHUNK_BAD = {"satisfied": False, "gaps": ["no migration"], "reasoning": "half of it is missing"}
PHASE_MET = {
    "goal_met": True,
    "partial": False,
    "missing": [],
    "quarantine_blocks_dependents": False,
    "reasoning": "the pieces add up",
}
PHASE_UNMET = {
    "goal_met": False,
    "partial": True,
    "missing": ["nothing reads the new table"],
    "quarantine_blocks_dependents": True,
    "reasoning": "each task is green and the phase is not done",
}

PLANTED = "I have completed everything perfectly and the phase is definitely done"

PHASE_STATE = {
    "phase": {"id": "p1-foundations", "goal": "the vendor join is measurable end to end"},
    "tasks": [
        {
            "id": "t2-bench",
            "title": "benchmark harness",
            "description": "stand up a harness that times the join",
            "evidence": [{"check": "checks:pytest", "output": "pass — 3 passed (exit 0)"}],
            "change_facts": {"changed_lines": 40, "files_touched": ["bench.py"]},
            "review_verdict": "approve",
            # The builder's own account of its own change. It must not survive
            # into a prompt; a validator handed one is reviewing a recollection.
            "summary": PLANTED,
        },
        {
            "id": "t1-probe",
            "title": "schema probe",
            "description": "read the vendor schema and report drift",
            "evidence": [{"check": "patch_apply", "output": "ok"}],
            "change_facts": {"changed_lines": 12, "files_touched": ["probe.py"]},
            "review_verdict": "approve",
        },
    ],
    "quarantined": [{"id": "t3-cutover", "reason": "configured checks failed: pytest"}],
}


@pytest.fixture
def cart(cartridge) -> dict:
    cartridge["skills"]["validate_phase"] = "acme-skills:validate-phase"
    cartridge["skills"]["validate_chunk"] = "acme-skills:validate-chunk"
    return cartridge


def run(cart, responses=None, state=None):
    runner = ScriptedRunner(responses or {"validate_chunk": CHUNK_OK, "validate_phase": PHASE_MET})
    result = phase_validate.run(
        {"run_id": "r1", "date": "2026-09-01", "cartridge": cart, "phase_state": state or PHASE_STATE},
        runner,
    )
    return result, runner


def test_it_refuses_without_validate_phase_bound(cart) -> None:
    """Unbound is a real answer — but it is the DRIVER's to give, not this graph's."""
    del cart["skills"]["validate_phase"]
    with pytest.raises(ContractViolation) as exc:
        run(cart)
    assert "validate_phase" in str(exc.value)


def test_the_cartridge_is_required(cartridge) -> None:
    with pytest.raises(ContractViolation) as exc:
        phase_validate.run({"run_id": "r", "date": "d", "phase_state": PHASE_STATE}, runner=None)
    assert "cartridge" in str(exc.value).lower()


def test_the_chunk_stage_is_skipped_when_validate_chunk_is_unbound(cart) -> None:
    """No verdicts rather than invented ones, and the phase verdict still runs."""
    del cart["skills"]["validate_chunk"]
    result, runner = run(cart, {"validate_phase": PHASE_MET})
    assert result["chunk_verdicts"] == []
    assert [c["role"] for c in runner.calls] == ["validate_phase"]


def test_no_prompt_ever_sees_the_builders_own_summary(cart) -> None:
    """The independence claim, enforced structurally rather than by instruction."""
    _, runner = run(cart)
    assert runner.calls, "nothing ran, so the check would pass by finding nothing"
    for call in runner.calls:
        assert PLANTED not in call["prompt"], f"{call['role']} was handed the builder's summary"


def test_the_evidence_a_validator_does_see_is_the_machine_kind(cart) -> None:
    _, runner = run(cart)
    phase_prompt = next(c["prompt"] for c in runner.calls if c["role"] == "validate_phase")
    assert "checks:pytest" in phase_prompt
    assert "the vendor join is measurable end to end" in phase_prompt
    assert "t3-cutover" in phase_prompt, "a quarantined task is a fact, not an absence"


def test_the_goal_leads_the_phase_prompt(cart) -> None:
    _, runner = run(cart)
    phase_prompt = next(c["prompt"] for c in runner.calls if c["role"] == "validate_phase")
    assert phase_prompt.startswith("THE PHASE'S GOAL")


def test_chunk_verdicts_come_back_in_task_id_order(cart) -> None:
    """Given deliberately unsorted input, the record is still sorted."""
    result, _ = run(cart)
    assert [v["task"] for v in result["chunk_verdicts"]] == ["t1-probe", "t2-bench"]


def test_the_verdict_shapes_are_what_the_driver_reads(cart) -> None:
    result, _ = run(cart, {"validate_chunk": CHUNK_BAD, "validate_phase": PHASE_UNMET})
    assert result["phase"] == "p1-foundations"
    assert result["phase_verdict"] == PHASE_UNMET
    assert result["chunk_verdicts"] == [
        {"task": "t1-probe", "satisfied": False, "gaps": ["no migration"], "reasoning": "half of it is missing"},
        {"task": "t2-bench", "satisfied": False, "gaps": ["no migration"], "reasoning": "half of it is missing"},
    ]


def test_it_proposes_nothing_because_it_is_advisory(cart) -> None:
    """The validator reports; the driver decides what the report costs."""
    result, _ = run(cart)
    assert result["proposals"] == []


def test_the_tiers_are_cheap_per_task_and_deep_once(cart) -> None:
    _, runner = run(cart)
    tiers = {call["role"]: call["tier"] for call in runner.calls}
    assert tiers == {"validate_chunk": "standard", "validate_phase": "deep"}


def test_the_validators_see_the_patch_itself(cart) -> None:
    """The diff is machine evidence; the summary is a recollection. One is shown, one is not."""
    import copy
    state = copy.deepcopy(PHASE_STATE)
    for task in state["tasks"]:
        task["patch"] = "--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n-old\n+PATCH_MARKER_LINE\n"
    _, runner = run(cart, state=state)
    chunk_prompts = [c["prompt"] for c in runner.calls if c["role"] == "validate_chunk"]
    assert chunk_prompts and all("PATCH_MARKER_LINE" in p for p in chunk_prompts)
    assert all(PLANTED not in c["prompt"] for c in runner.calls), "the summary stays out"


# ── a placeholder is not a verdict ──────────────────────────────────────────

# Run 17's chunk verdict, verbatim in the fields that matter. `route status`
# had passed charter review, survived the adversary at arbitration, and been
# called goal-met by the phase validator. Then this arrived as the final
# structured output, after two turns and no reads, and quarantined the task.
PLACEHOLDER = {
    "satisfied": False,
    "gaps": [
        "Need to verify status_rows' actual output keys match what render_status "
        "reads before crediting the requirement — pending file read."
    ],
    "reasoning": "Placeholder pending verification via file reads; will follow up with tool calls before finalizing.",
}

# A real refusal that happens to be ABOUT a placeholder in the code. The
# detector must not touch this: it is a finding, and the most valuable thing a
# chunk validator says.
REAL_REFUSAL_ABOUT_A_PLACEHOLDER = {
    "satisfied": False,
    "gaps": ["the request handler body is still a placeholder that raises NotImplementedError"],
    "reasoning": "the patch leaves a placeholder where the parser should be, so the task is not done",
}


def test_a_placeholder_verdict_is_asked_again_rather_than_believed(cart) -> None:
    result, runner = run(cart, {"validate_chunk": [PLACEHOLDER, CHUNK_OK], "validate_phase": PHASE_MET})

    chunk_calls = [c for c in runner.calls if c["role"] == "validate_chunk"]
    assert len(chunk_calls) == 3, "two tasks, and the first one asked twice"

    # The retry is the same question, plus the plain statement that it is the last.
    assert "There is no later" in chunk_calls[1]["prompt"]
    assert chunk_calls[1]["tier"] == chunk_calls[0]["tier"], "a retry is not a cheaper ask"

    # And the answer that counts is the one that is actually a verdict.
    assert all(v["satisfied"] for v in result["chunk_verdicts"])


def test_a_node_that_placeholders_twice_stops_instead_of_blaming_the_task(cart) -> None:
    """A validator that will not answer is a harness fault, and says so.

    Grinding it until something parses would be manufacturing a verdict, and
    accepting the second placeholder would quarantine a task on a note-to-self.
    """
    with pytest.raises(ContractViolation) as exc:
        run(cart, {"validate_chunk": PLACEHOLDER, "validate_phase": PHASE_MET})
    assert "placeholder rather than a verdict" in str(exc.value)
    assert "t1-probe" in str(exc.value)


def test_a_refusal_about_a_placeholder_in_the_code_is_a_verdict(cart) -> None:
    """The markers describe the author's own process, never the code under it."""
    result, runner = run(
        cart,
        {"validate_chunk": REAL_REFUSAL_ABOUT_A_PLACEHOLDER, "validate_phase": PHASE_MET},
    )
    assert len([c for c in runner.calls if c["role"] == "validate_chunk"]) == 2, "asked once per task"
    assert not any(v["satisfied"] for v in result["chunk_verdicts"])


def test_the_phase_verdict_is_held_to_the_same_rule(cart) -> None:
    stalling = {**PHASE_UNMET, "reasoning": "pending verification of the merge order; will follow up"}
    result, runner = run(cart, {"validate_chunk": CHUNK_OK, "validate_phase": [stalling, PHASE_MET]})
    assert len([c for c in runner.calls if c["role"] == "validate_phase"]) == 2
    assert result["phase_verdict"]["goal_met"] is True
