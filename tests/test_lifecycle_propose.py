"""lifecycle-propose: proposes, never writes; and never invents a risk."""

from __future__ import annotations

import pytest

from graphs.delivery import lifecycle_propose
from graphs._contract import ContractViolation
from runner import ScriptedRunner


def args(cartridge, **overrides):
    return {"run_id": "run-1", "date": "2026-08-30", "ticket": "TICKET-1", "cartridge": cartridge, **overrides}


def runner(plan_response, build_response, review_response, **overrides):
    return ScriptedRunner(
        {"plan": plan_response, "build": build_response, "review_charter": review_response, **overrides}
    )


def test_runs_end_to_end_and_returns_the_documented_shape(
    cartridge, plan_response, build_response, review_response
) -> None:
    result = lifecycle_propose.run(args(cartridge), runner(plan_response, build_response, review_response))
    assert set(result) == {
        "run_id", "date", "ticket", "scope", "review_tier", "handoff", "adversary",
        "arbitration", "plan", "build", "review", "change_facts", "fix_loop", "proposals",
    }


def test_scoping_is_skipped_when_the_team_has_not_bound_the_role(
    cartridge, plan_response, build_response, review_response
) -> None:
    """`scope_epic` is optional. Unbound means absent, not broken."""
    assert "scope_epic" not in cartridge["skills"]
    result = lifecycle_propose.run(args(cartridge), runner(plan_response, build_response, review_response))
    assert result["scope"] is None
    assert [p["kind"] for p in result["proposals"]] == ["draft_pr_create"]


def test_nodes_ask_for_roles_and_tiers_never_skills_or_models(
    cartridge, plan_response, build_response, review_response
) -> None:
    scripted = runner(plan_response, build_response, review_response)
    lifecycle_propose.run(args(cartridge), scripted)
    assert [(c["role"], c["tier"]) for c in scripted.calls] == [
        ("plan", "standard"),
        ("build", "standard"),
        ("review_charter", "deep"),
    ]


def test_change_facts_are_counted_from_the_patch_not_asked_of_the_model(
    cartridge, plan_response, build_response, review_response
) -> None:
    result = lifecycle_propose.run(args(cartridge), runner(plan_response, build_response, review_response))
    facts = result["change_facts"]
    assert facts["added_lines"] == 2 and facts["removed_lines"] == 1
    assert facts["changed_lines"] == 3


def test_approved_review_emits_a_draft_pr_proposal_and_applies_nothing(
    cartridge, plan_response, build_response, review_response
) -> None:
    result = lifecycle_propose.run(args(cartridge), runner(plan_response, build_response, review_response))
    assert [p["kind"] for p in result["proposals"]] == ["draft_pr_create"]
    assert result["proposals"][0]["risk"] == "low", "risk must come off the taxonomy"
    assert result["build"]["patch"], "the patch is returned, never applied here"


def test_rejected_review_proposes_nothing(cartridge, plan_response, build_response) -> None:
    rejected = {"verdict": "reject", "findings": [], "rationale": "violates the charter"}
    result = lifecycle_propose.run(args(cartridge), runner(plan_response, build_response, rejected))
    assert result["proposals"] == []


def test_every_proposal_carries_evidence(cartridge, plan_response, build_response, review_response) -> None:
    result = lifecycle_propose.run(args(cartridge), runner(plan_response, build_response, review_response))
    evidence = result["proposals"][0]["evidence"]
    assert evidence, "a claim without evidence is a guess with formatting"
    assert {"check": "pytest -q", "output": "1 passed"} in evidence, "deterministic checks, not prose"


def test_evidence_entries_share_one_shape(cartridge, plan_response, build_response, review_response) -> None:
    """The gate and the manifest both read `check`/`output`; a stray key prints as None."""
    result = lifecycle_propose.run(args(cartridge), runner(plan_response, build_response, review_response))
    for item in result["proposals"][0]["evidence"]:
        assert set(item) == {"check", "output"}, f"evidence entry has the wrong shape: {item}"
        assert item["check"] is not None


def test_refuses_a_write_kind_the_cartridge_never_declared(
    cartridge, plan_response, build_response, review_response
) -> None:
    del cartridge["write_kinds"]["draft_pr_create"]
    with pytest.raises(ContractViolation, match="unknown write kind 'draft_pr_create'"):
        lifecycle_propose.run(args(cartridge), runner(plan_response, build_response, review_response))


def test_refuses_a_raw_unresolved_cartridge(cartridge, plan_response, build_response, review_response) -> None:
    del cartridge["cartridge_sha"]
    with pytest.raises(ContractViolation, match="must be a RESOLVED cartridge"):
        lifecycle_propose.run(args(cartridge), runner(plan_response, build_response, review_response))


def test_names_every_missing_arg_at_once(cartridge, plan_response, build_response, review_response) -> None:
    incomplete = {"cartridge": cartridge, "run_id": "run-1"}
    with pytest.raises(ContractViolation) as exc:
        lifecycle_propose.run(incomplete, runner(plan_response, build_response, review_response))
    assert "date" in str(exc.value) and "ticket" in str(exc.value)


def test_context_packs_are_passed_to_nodes_never_read_by_the_graph(
    cartridge, plan_response, build_response, review_response
) -> None:
    cartridge["context"] = ["/fake/base/conventions.md", "/fake/acme/code-style.md"]
    scripted = runner(plan_response, build_response, review_response)
    lifecycle_propose.run(args(cartridge), scripted)
    assert all(call["context"] == cartridge["context"] for call in scripted.calls)


# ── the bounded fix loop ───────────────────────────────────────────────────
#
# The conviction under test: a fix loop must never launder struggle into trust.
# A task that passed on attempt three stays distinguishable from one that passed
# clean — and the loop stops on its own when a retry is not actually a retry.

PATCH_ANSWERED = (
    "--- a/src/a.py\n+++ b/src/a.py\n-old line\n"
    "+new line, now with the objection answered\n+another\n+assert covered()\n"
)
PATCH_ELSEWHERE = (
    "--- a/src/a.py\n+++ b/src/a.py\n-old line\n+new line\n+another\n"
    "+assert retry_path_is_tested()\n"
)
# The same patch with a trailing space added: 0.99 similar, and nothing that
# matters has changed.
PATCH_COSMETIC = "--- a/src/a.py\n+++ b/src/a.py\n-old line\n+new line\n+another \n"

REVISE = {"verdict": "revise", "findings": [], "rationale": "the error path is untested"}
OBJECTION = "the retry path has no test"
ADV_OBJECTS = {
    "verdict": "revise",
    "objections": [{"claim": OBJECTION, "why_wrong": "the only test covers the happy path"}],
    "strongest_objection": OBJECTION,
}
ADV_OBJECTS_AGAIN = {
    "verdict": "revise",
    # Same complaint, typed differently. Case and whitespace are not the objection.
    "objections": [{"claim": "  The Retry Path Has No Test  ", "why_wrong": "still only the happy path"}],
    "strongest_objection": "the retry path still has no test",
}
ADV_OBJECTS_ELSEWHERE = {
    "verdict": "revise",
    "objections": [{"claim": "the fixture leaks state", "why_wrong": "it mutates a module global"}],
    "strongest_objection": "the fixture leaks state",
}
ADV_APPROVES = {"verdict": "approve", "objections": [], "strongest_objection": "none that survive"}


def adversarial(cartridge) -> dict:
    """Bind the adversary, so a round can actually raise an objection."""
    cartridge["skills"]["review_adversary"] = "acme-skills:review_adversary"
    return cartridge


def rebuilt(build_response, patch) -> dict:
    return {**build_response, "patch": patch, "summary": "second attempt"}


def roles(scripted, role):
    return [call for call in scripted.calls if call["role"] == role]


def test_a_first_try_approval_records_one_attempt_and_carries_no_count(
    cartridge, plan_response, build_response, review_response
) -> None:
    """Catches the loop taxing every clean pass with a field about a loop that never ran.

    A proposal that always says `attempts` says nothing when it matters.
    """
    result = lifecycle_propose.run(args(cartridge), runner(plan_response, build_response, review_response))
    assert result["fix_loop"] == {"attempts": 1, "stopped": None}
    proposal = result["proposals"][0]
    assert "attempts" not in proposal, "a first-try pass looks exactly as it did before the loop existed"
    assert "fix loop" not in {e["check"] for e in proposal["evidence"]}


def test_a_change_sent_back_is_rebuilt_with_the_critique_and_can_pass_on_the_retry(
    cartridge, plan_response, build_response, review_response
) -> None:
    """Catches a retry that rebuilds from the plan alone, and a pass that hides its count.

    A builder handed 'review asked for changes' fixes what it already believed
    was wrong. It has to be handed the objection itself.
    """
    scripted = runner(
        plan_response,
        [build_response, rebuilt(build_response, PATCH_ANSWERED)],
        [REVISE, review_response],
        review_adversary=[ADV_OBJECTS, ADV_APPROVES],
    )
    result = lifecycle_propose.run(args(adversarial(cartridge)), scripted)

    builds = roles(scripted, "build")
    assert len(builds) == 2, "the change was sent back, so it must actually be rebuilt"
    assert OBJECTION in builds[1]["prompt"], "the retry carries the standing objection verbatim"
    assert "must actually fall" in builds[1]["prompt"]
    assert build_response["patch"] in builds[1]["prompt"], (
        "the retry starts from the previous patch — a builder that has to redo the whole "
        "task to answer one objection is the retry that blew the budget on the sixth live run"
    )
    assert "apply it first" in builds[1]["prompt"]

    assert result["fix_loop"] == {"attempts": 2, "stopped": None}
    proposal = result["proposals"][0]
    assert proposal["kind"] == "draft_pr_create"
    assert proposal["attempts"] == 2, "the ledger cannot discount what it is never told"
    assert {"check": "fix loop", "output": "approved on attempt 2 of 3"} in proposal["evidence"]
    assert result["build"]["patch"] == PATCH_ANSWERED, "the final round's build is the one that went out"


def test_a_retry_that_changes_nothing_stops_instead_of_buying_a_second_opinion(
    cartridge, plan_response, build_response
) -> None:
    """Catches a loop that re-reviews a patch it has already reviewed.

    Re-submitting the same diff to a fresh reviewer is not a fix; it is shopping
    for a verdict, and eventually one of them says yes.
    """
    scripted = runner(
        plan_response,
        [build_response, rebuilt(build_response, PATCH_COSMETIC)],
        REVISE,
        review_adversary=ADV_OBJECTS,
    )
    result = lifecycle_propose.run(args(adversarial(cartridge)), scripted)

    assert result["fix_loop"] == {"attempts": 2, "stopped": "no_progress"}
    assert len(roles(scripted, "review_charter")) == 1, "the near-identical patch was never reviewed"
    assert result["proposals"] == []
    assert result["build"]["patch"] == build_response["patch"], (
        "build and review must describe the same patch, or the record lies about what was reviewed"
    )


def test_the_same_objection_raised_again_stops_the_loop(
    cartridge, plan_response, build_response
) -> None:
    """Catches a loop that re-litigates one objection until the cap runs out.

    Matched case-insensitively and stripped: the same complaint typed
    differently is still the same complaint, still standing.
    """
    scripted = runner(
        plan_response,
        [build_response, rebuilt(build_response, PATCH_ANSWERED)],
        REVISE,
        review_adversary=[ADV_OBJECTS, ADV_OBJECTS_AGAIN],
    )
    result = lifecycle_propose.run(args(adversarial(cartridge)), scripted)

    assert result["fix_loop"] == {"attempts": 2, "stopped": "objection_standing"}
    assert len(roles(scripted, "build")) == 2, "it stopped rather than spending the second retry"
    assert result["proposals"] == []


def test_the_cap_is_a_cap_and_an_unapproved_change_proposes_nothing(
    cartridge, plan_response, build_response
) -> None:
    """Catches a loop that grinds a change past its reviewers until one blinks."""
    scripted = runner(
        plan_response,
        [build_response, rebuilt(build_response, PATCH_ANSWERED), rebuilt(build_response, PATCH_ELSEWHERE)],
        REVISE,
        review_adversary=[ADV_OBJECTS, ADV_OBJECTS_ELSEWHERE],
    )
    result = lifecycle_propose.run(args(adversarial(cartridge), fix_attempts=1), scripted)

    assert result["fix_loop"] == {"attempts": 2, "stopped": "attempts_exhausted"}
    assert len(roles(scripted, "build")) == 2, "one additional attempt means one, not one more each round"
    assert [p["kind"] for p in result["proposals"]] == [], "nothing approved, so nothing proposed"


def test_fix_attempts_zero_disables_the_loop_entirely(
    cartridge, plan_response, build_response
) -> None:
    """Catches a cap of zero that still retries once — an off switch that is not off."""
    scripted = runner(plan_response, build_response, REVISE, review_adversary=ADV_OBJECTS)
    result = lifecycle_propose.run(args(adversarial(cartridge), fix_attempts=0), scripted)

    assert len(roles(scripted, "build")) == 1
    assert result["fix_loop"] == {"attempts": 1, "stopped": "attempts_exhausted"}
    assert result["proposals"] == []


def test_plan_build_and_retry_share_a_thread_and_review_never_does(
    cartridge, plan_response, build_response, review_response
) -> None:
    """Continuity is for the maker. A reviewer that inherits the builder's session
    inherits its reasoning, which is the independence the seat exists for."""
    scripted = runner(
        plan_response,
        [build_response, rebuilt(build_response, PATCH_ANSWERED)],
        [REVISE, review_response],
        review_adversary=[ADV_OBJECTS, ADV_APPROVES],
    )
    lifecycle_propose.run(args(adversarial(cartridge)), scripted)
    threads = {(c["role"], c["thread"]) for c in scripted.calls}
    assert ("plan", "TICKET-1") in threads
    assert all(c["thread"] == "TICKET-1" for c in roles(scripted, "build")), "both builds, first and retry"
    for role in ("review_charter", "review_adversary", "arbitrate", "handoff"):
        assert all(c["thread"] is None for c in roles(scripted, role)), role


def test_the_work_items_words_travel_with_its_id(
    cartridge, plan_response, build_response, review_response
) -> None:
    """A plan node given only 'wake-phrase-env' globbed the repository for a file
    by that name. Given the title and body, it plans."""
    scripted = runner(plan_response, build_response, review_response)
    lifecycle_propose.run(
        args(cartridge, ticket_title="Make the wake phrase configurable", ticket_body="WAKE_WORDS is a literal tuple; read VOICE_HUD_WAKE_PHRASES instead."),
        scripted,
    )
    plan = roles(scripted, "plan")[0]["prompt"]
    assert "TICKET-1 — Make the wake phrase configurable" in plan
    assert "VOICE_HUD_WAKE_PHRASES" in plan
    review = roles(scripted, "review_charter")[0]["prompt"]
    assert "Make the wake phrase configurable" in review, "reviewers judge against the ask, not the id"


def test_without_title_or_body_the_id_stands_alone(
    cartridge, plan_response, build_response, review_response
) -> None:
    scripted = runner(plan_response, build_response, review_response)
    lifecycle_propose.run(args(cartridge), scripted)
    assert "Ticket: TICKET-1\n" in roles(scripted, "plan")[0]["prompt"]
