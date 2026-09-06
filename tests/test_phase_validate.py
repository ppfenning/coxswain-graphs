"""The two validators, and the independence that makes the second one worth having.

`validate_phase` exists to catch five individually-green tasks that do not add
up. It can only do that if it is not reading the builder's own account of the
work — so the property under test is not merely that the graph runs, it is that
a planted summary NEVER reaches a prompt.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from graphs._contract import ContractViolation
from graphs.delivery import phase_validate
from runner import ScriptedRunner

_UNSET = object()


def _fs_reader(worktree: str, rel: str) -> str | None:
    """A reader double standing in for the one `harness/epic.py` injects."""
    try:
        return Path(worktree, rel).read_text()
    except OSError:
        return None

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


def run(cart, responses=None, state=None, worktree=None, reader=_UNSET):
    """`worktree`, given, is stamped onto every task; `reader` defaults to
    `_fs_reader` whenever a worktree is given, and is omitted otherwise — pass
    `reader=None` explicitly to simulate the harness supplying no reader at all.
    """
    runner = ScriptedRunner(responses or {"validate_chunk": CHUNK_OK, "validate_phase": PHASE_MET})
    phase_state = copy.deepcopy(state or PHASE_STATE)
    if worktree is not None:
        for task in phase_state["tasks"]:
            task["worktree"] = str(worktree)
    args = {"run_id": "r1", "date": "2026-09-01", "cartridge": cart, "phase_state": phase_state}
    if reader is not _UNSET:
        args["reader"] = reader
    elif worktree is not None:
        args["reader"] = _fs_reader
    result = phase_validate.run(args, runner)
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
    """A chunk validator that will not answer refuses about itself, not the task.

    The epic driver invokes this graph once for the whole phase. Raising here
    would lose the sibling task's verdict to one task's placeholder — before
    this graph retried, the same event quarantined one task, and a retry must
    never make the failure larger than that.
    """
    result, runner = run(
        cart,
        {"validate_chunk": [PLACEHOLDER, PLACEHOLDER, CHUNK_OK], "validate_phase": PHASE_MET},
    )

    chunk_calls = [c for c in runner.calls if c["role"] == "validate_chunk"]
    assert len(chunk_calls) == 3, "two placeholders for one task, one real answer for the other"
    assert sum(1 for c in chunk_calls if "t1-probe" in c["prompt"]) == 2

    by_task = {v["task"]: v for v in result["chunk_verdicts"]}
    assert by_task["t2-bench"]["satisfied"] is True, "a sibling's verdict survives another task's fault"
    assert by_task["t1-probe"]["satisfied"] is False
    assert "harness fault" in " ".join(by_task["t1-probe"]["gaps"])


@pytest.mark.parametrize(
    "marker",
    [
        "need to verify",
        "would need to read",
        "cannot confirm from the evidence provided",
        "placeholder, will",
    ],
)
def test_each_new_marker_is_asked_again_rather_than_believed(cart, marker) -> None:
    stalling = {"satisfied": False, "gaps": [], "reasoning": f"{marker} the migration before ruling on this."}
    result, runner = run(cart, {"validate_chunk": [stalling, CHUNK_OK], "validate_phase": PHASE_MET})
    chunk_calls = [c for c in runner.calls if c["role"] == "validate_chunk"]
    assert len(chunk_calls) == 3, "two tasks, and the first one asked twice"
    assert all(v["satisfied"] for v in result["chunk_verdicts"])


@pytest.mark.parametrize("phrase", ["needs to verify", "will redo"])
def test_a_real_gap_using_third_person_verify_or_redo_is_not_a_placeholder(cart, phrase) -> None:
    """These two read naturally inside a genuine finding, so they are not markers.

    Left out of `_PLACEHOLDER_MARKERS`, unlike first-person 'need to verify':
    a gap that repeats one of these twice is still a real gap, not a harness
    fault, and must not quarantine the task.
    """
    real_gap = {
        "satisfied": False,
        "gaps": [f"the loader {phrase} the row count before the write"],
        "reasoning": "the migration is missing a check",
    }
    result, runner = run(cart, {"validate_chunk": [real_gap, real_gap], "validate_phase": PHASE_MET})
    chunk_calls = [c for c in runner.calls if c["role"] == "validate_chunk"]
    assert len(chunk_calls) == 2, "asked once per task; the repeated gap was never a placeholder"
    by_task = {v["task"]: v for v in result["chunk_verdicts"]}
    assert not by_task["t1-probe"]["satisfied"]
    assert phrase in " ".join(by_task["t1-probe"]["gaps"]), "the real gap survives, not a harness-fault stand-in"


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


def test_a_placeholdering_phase_verdict_still_raises(cart) -> None:
    """There is nothing narrower to fall back to for the phase verdict itself.

    Unlike the chunk case, a phase with no verdict is exactly what the record
    should say happened.
    """
    stalling = {**PHASE_UNMET, "reasoning": "provisional verdict; will verify after reading the files"}
    with pytest.raises(ContractViolation) as exc:
        run(cart, {"validate_chunk": CHUNK_OK, "validate_phase": stalling})
    assert "placeholder rather than a verdict" in str(exc.value)
    assert "p1-foundations" in str(exc.value)


# ── evidence_requests: normalising the ask ──────────────────────────────────


def test_evidence_requests_keeps_a_typed_object_as_is() -> None:
    kept, dropped = phase_validate.evidence_requests(
        [{"path": "core/manifest.py", "why": "confirm the sha assignment"}]
    )
    assert kept == [{"path": "core/manifest.py", "why": "confirm the sha assignment"}]
    assert dropped == []


def test_evidence_requests_reads_a_bare_string_as_a_path() -> None:
    kept, dropped = phase_validate.evidence_requests(["core/manifest.py"])
    assert kept == [{"path": "core/manifest.py"}]
    assert dropped == []


def test_evidence_requests_drops_an_entry_with_no_usable_path() -> None:
    entry = {"why": "confirm overlay_sha comes from the same resolved-cartridge mapping"}
    kept, dropped = phase_validate.evidence_requests([entry])
    assert kept == []
    assert dropped == [entry]


# ── needs_evidence: a typed ask, fulfilled once, and never refused by the harness itself ────

CHUNK_NEEDS_EVIDENCE = {
    "satisfied": False,
    "gaps": [],
    "reasoning": "cannot tell without reading the migration",
    "needs_evidence": ["migrations/0007_add_col.sql"],
}

CHUNK_NEEDS_EVIDENCE_OUTSIDE = {
    "satisfied": False,
    "gaps": [],
    "reasoning": "cannot tell without reading a file outside the tree",
    "needs_evidence": ["../secrets.env"],
}

CHUNK_OK_WITH_NEEDS_EVIDENCE = {
    "satisfied": True,
    "gaps": [],
    "reasoning": "confirmed by the diff alone",
    "needs_evidence": ["some/file.py"],
}

SECOND_STILL_ASKS = {
    "satisfied": False,
    "gaps": ["still missing something"],
    "reasoning": "even with the file, it is incomplete",
    "needs_evidence": ["migrations/0008_more.sql"],
}

SECOND_RULES_WITHOUT_THE_FILE = {
    "satisfied": True,
    "gaps": [],
    "reasoning": "ruled on the patch alone since the file could not be supplied",
}

SECOND_REFUSES_ON_ITS_OWN_GAP = {
    "satisfied": False,
    "gaps": ["the join still drops rows with a null vendor_id"],
    "reasoning": "the patch alone shows a real gap, evidence or not",
}


def test_needs_evidence_asks_once_more_with_the_file_in_the_prompt(cart, tmp_path) -> None:
    """The follow-up carries the file's text in `prompt`, never in `context` — context entries are read as files."""
    target = tmp_path / "migrations" / "0007_add_col.sql"
    target.parent.mkdir(parents=True)
    target.write_text("ALTER TABLE vendor ADD COLUMN drift_flag boolean;\n")

    result, runner = run(
        cart,
        {"validate_chunk": [CHUNK_NEEDS_EVIDENCE, CHUNK_OK], "validate_phase": PHASE_MET},
        worktree=tmp_path,
    )
    chunk_calls = [c for c in runner.calls if c["role"] == "validate_chunk"]
    assert len(chunk_calls) == 3, "two tasks; one of them asks a follow-up"
    assert chunk_calls[1]["context"] == [], "the follow-up never smuggles prose into context"
    follow_up = chunk_calls[1]["prompt"]
    assert "Evidence you asked for" in follow_up
    assert "ALTER TABLE vendor ADD COLUMN drift_flag boolean;" in follow_up

    by_task = {v["task"]: v for v in result["chunk_verdicts"]}
    assert by_task["t1-probe"]["evidence_supplied"] == ["migrations/0007_add_col.sql"]
    assert by_task["t1-probe"]["satisfied"] is True, "the second verdict is final, as returned"


def test_an_unreadable_needs_evidence_with_a_satisfied_second_verdict_finishes_clean(cart, tmp_path) -> None:
    """A path the harness cannot read still gets a follow-up; it never becomes a refusal by itself."""
    result, runner = run(
        cart,
        {"validate_chunk": [CHUNK_NEEDS_EVIDENCE_OUTSIDE, SECOND_RULES_WITHOUT_THE_FILE], "validate_phase": PHASE_MET},
        worktree=tmp_path,
    )
    chunk_calls = [c for c in runner.calls if c["role"] == "validate_chunk"]
    assert len(chunk_calls) == 3, "the unreadable path still buys a follow-up, not a harness-made refusal"
    assert chunk_calls[1]["context"] == [], "the follow-up never smuggles prose into context"
    follow_up = chunk_calls[1]["prompt"]
    assert "../secrets.env" in follow_up and "outside the worktree" in follow_up

    by_task = {v["task"]: v for v in result["chunk_verdicts"]}
    assert by_task["t1-probe"] == {
        "task": "t1-probe",
        "satisfied": True,
        "gaps": [],
        "reasoning": "ruled on the patch alone since the file could not be supplied",
        "evidence_supplied": [],
        "evidence_unread": ["../secrets.env (outside the worktree)"],
    }


def test_an_unreadable_needs_evidence_with_a_refusing_second_verdict_keeps_only_its_own_gap(cart, tmp_path) -> None:
    result, runner = run(
        cart,
        {"validate_chunk": [CHUNK_NEEDS_EVIDENCE_OUTSIDE, SECOND_REFUSES_ON_ITS_OWN_GAP], "validate_phase": PHASE_MET},
        worktree=tmp_path,
    )
    by_task = {v["task"]: v for v in result["chunk_verdicts"]}
    assert by_task["t1-probe"]["satisfied"] is False
    assert by_task["t1-probe"]["gaps"] == ["the join still drops rows with a null vendor_id"]


def test_more_than_five_requested_paths_are_truncated_to_five_and_named_to_the_validator(cart, tmp_path) -> None:
    names = [f"f{i}.txt" for i in range(7)]
    for name in names:
        (tmp_path / name).write_text(f"contents of {name}\n")
    needs_seven = {
        "satisfied": False,
        "gaps": [],
        "reasoning": "need several files",
        "needs_evidence": names,
    }
    result, runner = run(
        cart,
        {"validate_chunk": [needs_seven, CHUNK_OK], "validate_phase": PHASE_MET},
        worktree=tmp_path,
    )
    second_call = [c for c in runner.calls if c["role"] == "validate_chunk"][1]
    assert second_call["context"] == [], "the follow-up never smuggles prose into context"
    follow_up = second_call["prompt"]
    assert sum(1 for name in names if f"contents of {name}" in follow_up) == 5
    assert "f5.txt" in follow_up and "f6.txt" in follow_up and "cap" in follow_up

    by_task = {v["task"]: v for v in result["chunk_verdicts"]}
    assert by_task["t1-probe"]["gaps"] == [], "the overflow is named to the validator, not baked into the outcome"


def test_a_second_needs_evidence_is_not_chased(cart, tmp_path) -> None:
    target = tmp_path / "migrations" / "0007_add_col.sql"
    target.parent.mkdir(parents=True)
    target.write_text("ALTER TABLE vendor ADD COLUMN drift_flag boolean;\n")

    result, runner = run(
        cart,
        {"validate_chunk": [CHUNK_NEEDS_EVIDENCE, SECOND_STILL_ASKS, CHUNK_OK], "validate_phase": PHASE_MET},
        worktree=tmp_path,
    )
    chunk_calls = [c for c in runner.calls if c["role"] == "validate_chunk"]
    assert len(chunk_calls) == 3, "one follow-up only, whatever the follow-up itself answers"

    by_task = {v["task"]: v for v in result["chunk_verdicts"]}
    assert by_task["t1-probe"] == {
        "task": "t1-probe",
        "satisfied": False,
        "gaps": ["still missing something"],
        "reasoning": "even with the file, it is incomplete",
        "evidence_supplied": ["migrations/0007_add_col.sql"],
    }


def test_a_satisfied_verdict_never_gets_a_second_call_even_with_needs_evidence(cart, tmp_path) -> None:
    _, runner = run(
        cart,
        {"validate_chunk": CHUNK_OK_WITH_NEEDS_EVIDENCE, "validate_phase": PHASE_MET},
        worktree=tmp_path,
    )
    chunk_calls = [c for c in runner.calls if c["role"] == "validate_chunk"]
    assert len(chunk_calls) == 2, "one ask per task; satisfied needs no follow-up"


def test_no_worktree_on_the_task_still_gets_a_second_ask_naming_the_gap(cart) -> None:
    """The graph itself opens nothing; with no worktree it still asks again, in the prompt, naming why."""
    result, runner = run(cart, {"validate_chunk": [CHUNK_NEEDS_EVIDENCE, CHUNK_OK], "validate_phase": PHASE_MET})
    chunk_calls = [c for c in runner.calls if c["role"] == "validate_chunk"]
    assert len(chunk_calls) == 3, "one ask per task; a request the harness cannot resolve still buys a follow-up"
    assert chunk_calls[1]["context"] == [], "the follow-up never smuggles prose into context"
    assert "no worktree was supplied" in chunk_calls[1]["prompt"]

    by_task = {v["task"]: v for v in result["chunk_verdicts"]}
    assert by_task["t1-probe"]["evidence_supplied"] == []
    assert by_task["t1-probe"]["evidence_unread"] == ["migrations/0007_add_col.sql (no worktree was supplied)"]


def test_a_worktree_with_no_reader_still_gets_a_second_ask(cart, tmp_path) -> None:
    """A worktree names where to look; without a reader the graph still reads nothing itself, and still asks again."""
    result, runner = run(
        cart,
        {"validate_chunk": [CHUNK_NEEDS_EVIDENCE, CHUNK_OK], "validate_phase": PHASE_MET},
        worktree=tmp_path,
        reader=None,
    )
    chunk_calls = [c for c in runner.calls if c["role"] == "validate_chunk"]
    assert len(chunk_calls) == 3, "one ask per task; no reader still buys a follow-up"
    assert chunk_calls[1]["context"] == [], "the follow-up never smuggles prose into context"
    assert "no evidence reader was supplied" in chunk_calls[1]["prompt"]

    by_task = {v["task"]: v for v in result["chunk_verdicts"]}
    assert by_task["t1-probe"]["evidence_supplied"] == []
    assert by_task["t1-probe"]["evidence_unread"] == ["migrations/0007_add_col.sql (no evidence reader was supplied)"]
