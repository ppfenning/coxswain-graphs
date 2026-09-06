"""lifecycle-propose: proposes, never writes; and never invents a risk."""

from __future__ import annotations

import pytest

from graphs._contract import ContractViolation
from graphs.delivery import lifecycle_propose
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
        "arbitration", "plan", "plan_competition", "plan_attack", "plan_gate", "build", "review",
        "change_facts", "fix_loop", "proposals",
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
    assert result["fix_loop"] == {"attempts": 1, "stopped": None, "continuations": 0}
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

    assert result["fix_loop"] == {"attempts": 2, "stopped": None, "continuations": 0}
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

    assert result["fix_loop"] == {"attempts": 2, "stopped": "no_progress", "continuations": 0}
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

    assert result["fix_loop"] == {"attempts": 2, "stopped": "objection_standing", "continuations": 0}
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

    assert result["fix_loop"] == {"attempts": 2, "stopped": "attempts_exhausted", "continuations": 0}
    assert len(roles(scripted, "build")) == 2, "one additional attempt means one, not one more each round"
    assert [p["kind"] for p in result["proposals"]] == [], "nothing approved, so nothing proposed"


def test_fix_attempts_zero_disables_the_loop_entirely(
    cartridge, plan_response, build_response
) -> None:
    """Catches a cap of zero that still retries once — an off switch that is not off."""
    scripted = runner(plan_response, build_response, REVISE, review_adversary=ADV_OBJECTS)
    result = lifecycle_propose.run(args(adversarial(cartridge), fix_attempts=0), scripted)

    assert len(roles(scripted, "build")) == 1
    assert result["fix_loop"] == {"attempts": 1, "stopped": "attempts_exhausted", "continuations": 0}
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


# ── the plan competition ───────────────────────────────────────────────────
#
# The conviction under test: solutions compete where they are cheap. A second
# plan and a decision between two plans cost a fraction of a build, and an
# objection raised against a plan costs one more plan, not a rebuild.

PLAN_B = {"steps": ["add a guard in parse_row", "test the guard"], "files_expected": ["src/b.py"], "out_of_scope": ["src/a.py"]}
PLAN_MERGED = {"steps": ["read the failing test", "add a guard in parse_row", "test both"], "files_expected": ["src/a.py", "src/b.py"], "out_of_scope": ["the CLI"]}
PLAN_REVISED = {"steps": ["read the failing test", "fix parse_row, which is the function that exists"], "files_expected": ["src/a.py"], "out_of_scope": ["the CLI"]}
CHOOSE_FIRST_WITH_A_RESTATEMENT = {"chosen": "first", "plan": PLAN_MERGED, "reasoning": "a names the file the test imports", "price": "b's guard goes unwritten"}
CHOOSE_SECOND = {"chosen": "second", "plan": PLAN_B, "reasoning": "b's steps are checkable", "price": "a second file to review"}
CHOOSE_MERGED = {"chosen": "merged", "plan": PLAN_MERGED, "reasoning": "a's first step plus b's guard", "price": "two files instead of one"}
ATTACK_PROCEED = {"verdict": "proceed", "objections": [], "strongest_objection": "none that survive"}
ATTACK_REVISE = {
    "verdict": "revise",
    "objections": [{"claim": "the plan names a Reader class", "why_wrong": "src/a.py has parse_row and no Reader"}],
    "strongest_objection": "the plan names a Reader class that does not exist",
}


def competitive(cartridge) -> dict:
    cartridge["skills"]["plan_alternative"] = "acme-skills:plan"
    cartridge["skills"]["plan_arbitrate"] = "acme-skills:arbitrate-plans"
    return cartridge


def attacked(cartridge) -> dict:
    cartridge["skills"]["plan_adversary"] = "acme-skills:review_adversary"
    return cartridge


def test_an_unbound_competition_is_absent_not_broken(
    cartridge, plan_response, build_response, review_response
) -> None:
    scripted = runner(plan_response, build_response, review_response)
    result = lifecycle_propose.run(args(cartridge), scripted)
    assert [c["role"] for c in scripted.calls] == ["plan", "build", "review_charter"]
    assert result["plan_competition"] is None and result["plan_attack"] is None
    assert result["plan"] == plan_response
    assert "plan competition" not in {e["check"] for e in result["proposals"][0]["evidence"]}


def test_an_alternative_without_an_arbiter_is_never_asked_for(
    cartridge, plan_response, build_response, review_response
) -> None:
    """A second plan nobody judges is a plan nobody builds — and a budget spent."""
    cartridge["skills"]["plan_alternative"] = "acme-skills:plan"
    scripted = runner(plan_response, build_response, review_response, plan_alternative=PLAN_B)
    lifecycle_propose.run(args(cartridge), scripted)
    assert roles(scripted, "plan_alternative") == []


def test_the_chosen_plan_is_the_one_the_builder_gets(
    cartridge, plan_response, build_response, review_response
) -> None:
    scripted = runner(
        plan_response, build_response, review_response,
        plan_alternative=PLAN_B, plan_arbitrate=CHOOSE_SECOND,
    )
    result = lifecycle_propose.run(args(competitive(cartridge)), scripted)
    assert [(c["role"], c["tier"]) for c in scripted.calls][:4] == [
        ("plan", "standard"), ("plan_alternative", "standard"), ("plan_arbitrate", "deep"), ("build", "standard"),
    ]
    assert result["plan"] == PLAN_B
    assert "add a guard in parse_row" in roles(scripted, "build")[0]["prompt"]
    assert "read the failing test" not in roles(scripted, "build")[0]["prompt"], "the losing plan does not travel"
    assert result["plan_competition"]["chosen"] == "second"
    evidence = {e["check"]: e["output"] for e in result["proposals"][0]["evidence"]}
    assert evidence["plan competition"] == "chose second: b's steps are checkable (price: a second file to review)"


def test_a_pick_hands_the_source_plan_over_verbatim_not_the_arbiters_restatement(
    cartridge, plan_response, build_response, review_response
) -> None:
    """What was compared is what gets built. An arbiter that picks `first` and
    also returns its own improved plan has not been asked for the improvement."""
    scripted = runner(
        plan_response, build_response, review_response,
        plan_alternative=PLAN_B, plan_arbitrate=CHOOSE_FIRST_WITH_A_RESTATEMENT,
    )
    result = lifecycle_propose.run(args(competitive(cartridge)), scripted)
    assert result["plan"] == plan_response


def test_a_merge_is_the_arbiters_own_plan(
    cartridge, plan_response, build_response, review_response
) -> None:
    scripted = runner(
        plan_response, build_response, review_response,
        plan_alternative=PLAN_B, plan_arbitrate=CHOOSE_MERGED,
    )
    result = lifecycle_propose.run(args(competitive(cartridge)), scripted)
    assert result["plan"] == PLAN_MERGED
    assert result["plan_competition"]["alternative"] == PLAN_B, "the record keeps the loser"


def test_the_second_planner_and_the_arbiter_never_join_the_first_planners_thread(
    cartridge, plan_response, build_response, review_response
) -> None:
    """Independence is the whole value of a second plan."""
    scripted = runner(
        plan_response, build_response, review_response,
        plan_alternative=PLAN_B, plan_arbitrate=CHOOSE_SECOND,
    )
    lifecycle_propose.run(args(competitive(cartridge)), scripted)
    threads = {
        role: roles(scripted, role)[0]["thread"] for role in ("plan", "plan_alternative", "plan_arbitrate")
    }
    assert threads["plan"] == "TICKET-1"
    assert len(set(threads.values())) == 3, "three seats, three threads"
    assert all(thread for thread in threads.values()), "each seat keeps a thread a revision can continue"
    assert "do not repeat it" in roles(scripted, "plan_alternative")[0]["prompt"]


def test_the_plan_adversary_can_let_a_plan_proceed_untouched(
    cartridge, plan_response, build_response, review_response
) -> None:
    scripted = runner(plan_response, build_response, review_response, plan_adversary=ATTACK_PROCEED)
    result = lifecycle_propose.run(args(attacked(cartridge)), scripted)
    assert [c["role"] for c in scripted.calls] == ["plan", "plan_adversary", "build", "review_charter"]
    assert result["plan"] == plan_response
    assert result["plan_attack"] == {"attack": ATTACK_PROCEED, "revised": False}
    evidence = {e["check"]: e["output"] for e in result["proposals"][0]["evidence"]}
    assert evidence["plan adversary"] == "proceed — strongest: none that survive"


def test_the_plan_adversary_sends_a_plan_back_exactly_once(
    cartridge, plan_response, build_response, review_response
) -> None:
    """Catches a second fix loop growing at the front of the graph. One revision,
    carrying the objections verbatim, and the revision is what gets built — the
    review round after build is where it gets judged, not a second attack."""
    scripted = runner(
        [plan_response, PLAN_REVISED], build_response, review_response,
        plan_adversary=[ATTACK_REVISE, ATTACK_REVISE],
    )
    result = lifecycle_propose.run(args(attacked(cartridge)), scripted)
    assert [c["role"] for c in scripted.calls] == ["plan", "plan_adversary", "plan", "build", "review_charter"]
    revision = roles(scripted, "plan")[1]
    assert revision["thread"] == "TICKET-1", "the revision is the same planner, continuing"
    assert "src/a.py has parse_row and no Reader" in revision["prompt"]
    assert result["plan"] == PLAN_REVISED
    assert "which is the function that exists" in roles(scripted, "build")[0]["prompt"]
    assert result["plan_attack"]["revised"] is True
    evidence = {e["check"]: e["output"] for e in result["proposals"][0]["evidence"]}
    assert evidence["plan adversary"].endswith("— plan revised once")


def test_the_attack_targets_the_plan_the_competition_chose(
    cartridge, plan_response, build_response, review_response
) -> None:
    scripted = runner(
        plan_response, build_response, review_response,
        plan_alternative=PLAN_B, plan_arbitrate=CHOOSE_SECOND, plan_adversary=ATTACK_PROCEED,
    )
    lifecycle_propose.run(args(attacked(competitive(cartridge))), scripted)
    assert [c["role"] for c in scripted.calls][:5] == ["plan", "plan_alternative", "plan_arbitrate", "plan_adversary", "build"]
    assert "add a guard in parse_row" in roles(scripted, "plan_adversary")[0]["prompt"]


# A revision goes back to whoever wrote the plan, on that seat's own thread.
# The failure these three guard against: the competition chooses `second`, the
# adversary says revise, and the first planner — on a thread already holding
# its own losing plan — is handed the second planner's plan and told it is its
# own.


def test_a_revision_of_the_first_plan_goes_back_to_the_first_planner(
    cartridge, plan_response, build_response, review_response
) -> None:
    scripted = runner(
        [plan_response, PLAN_REVISED], build_response, review_response,
        plan_alternative=PLAN_B, plan_arbitrate=CHOOSE_FIRST_WITH_A_RESTATEMENT, plan_adversary=ATTACK_REVISE,
    )
    result = lifecycle_propose.run(args(attacked(competitive(cartridge))), scripted)
    assert [c["role"] for c in scripted.calls][:6] == [
        "plan", "plan_alternative", "plan_arbitrate", "plan_adversary", "plan", "build",
    ]
    revision = roles(scripted, "plan")[1]
    assert revision["thread"] == "TICKET-1", "the first planner, continuing on its own thread"
    assert "read the failing test" in revision["prompt"] and "add a guard in parse_row" not in revision["prompt"]
    assert len(roles(scripted, "plan_alternative")) == 1 and len(roles(scripted, "plan_arbitrate")) == 1
    assert result["plan"] == PLAN_REVISED


def test_a_revision_of_the_second_plan_goes_back_to_the_second_planner_on_its_own_thread(
    cartridge, plan_response, build_response, review_response
) -> None:
    """The first planner's thread already holds the plan that lost. Handing it
    the winner and calling that a revision is neither planner continuing."""
    scripted = runner(
        plan_response, build_response, review_response,
        plan_alternative=[PLAN_B, PLAN_REVISED], plan_arbitrate=CHOOSE_SECOND, plan_adversary=ATTACK_REVISE,
    )
    result = lifecycle_propose.run(args(attacked(competitive(cartridge))), scripted)
    assert [c["role"] for c in scripted.calls][:6] == [
        "plan", "plan_alternative", "plan_arbitrate", "plan_adversary", "plan_alternative", "build",
    ]
    original, revision = roles(scripted, "plan_alternative")
    assert revision["thread"] == original["thread"], "the second planner, continuing"
    assert revision["thread"] != "TICKET-1", "never the first planner's thread"
    assert "add a guard in parse_row" in revision["prompt"]
    assert "src/a.py has parse_row and no Reader" in revision["prompt"]
    assert len(roles(scripted, "plan")) == 1, "the first planner is not asked to revise a plan it did not write"
    assert result["plan"] == PLAN_REVISED
    assert result["plan_attack"]["revised"] is True


def test_a_revision_of_a_merged_plan_goes_back_to_the_arbiter_not_to_either_planner(
    cartridge, plan_response, build_response, review_response
) -> None:
    """A merge is the arbiter's plan. Neither planner wrote it, so neither is
    the author a revision can go back to."""
    scripted = runner(
        plan_response, build_response, review_response,
        plan_alternative=PLAN_B, plan_arbitrate=[CHOOSE_MERGED, PLAN_REVISED], plan_adversary=ATTACK_REVISE,
    )
    result = lifecycle_propose.run(args(attacked(competitive(cartridge))), scripted)
    assert [c["role"] for c in scripted.calls][:6] == [
        "plan", "plan_alternative", "plan_arbitrate", "plan_adversary", "plan_arbitrate", "build",
    ]
    decision, revision = roles(scripted, "plan_arbitrate")
    assert revision["thread"] == decision["thread"], "the arbiter, continuing"
    assert revision["thread"] not in ("TICKET-1", roles(scripted, "plan_alternative")[0]["thread"])
    assert revision["tier"] == "deep"
    assert "test both" in revision["prompt"], "the merged plan is what gets revised"
    assert len(roles(scripted, "plan")) == 1 and len(roles(scripted, "plan_alternative")) == 1
    assert result["plan"] == PLAN_REVISED


def test_a_pick_does_not_require_the_arbiter_to_write_a_plan(
    cartridge, plan_response, build_response, review_response
) -> None:
    """A plan emitted alongside a pick is a plan nobody reads."""
    assert "plan" not in lifecycle_propose.PLAN_CHOICE_SCHEMA["required"]
    scripted = runner(
        plan_response, build_response, review_response,
        plan_alternative=PLAN_B, plan_arbitrate={"chosen": "second", "reasoning": "b's steps are checkable", "price": "a second file"},
    )
    result = lifecycle_propose.run(args(competitive(cartridge)), scripted)
    assert result["plan"] == PLAN_B


def test_a_merge_without_a_plan_stops_the_graph_instead_of_building_the_first_plan(
    cartridge, plan_response, build_response, review_response
) -> None:
    """A merge is the arbiter's own plan; an arbiter that says `merged` and
    writes nothing has claimed a plan that does not exist. Quietly building the
    first plan under that claim would send a later revision to the arbiter
    carrying a plan it never wrote."""
    scripted = runner(
        plan_response, build_response, review_response,
        plan_alternative=PLAN_B, plan_arbitrate={"chosen": "merged", "reasoning": "both halves", "price": "two files"},
    )
    with pytest.raises(ContractViolation, match="claimed 'merged'"):
        lifecycle_propose.run(args(competitive(cartridge)), scripted)
    assert roles(scripted, "build") == [], "no plan was built under the false claim"


# ── a review is not a verdict when it only names what it would check ───────

# The literal charter-reviewer answer that burned two build attempts: a
# finding whose detail is the bare word "placeholder" and a rationale that is
# just the empty object a schema-shaped stub produces.
INCIDENT_LITERAL = {
    "verdict": "revise",
    "findings": [{"detail": "placeholder", "charter_principle": "handoff evidence", "file": ""}],
    "rationale": "{}",
}


def test_review_is_placeholder_catches_the_literal_incident() -> None:
    assert lifecycle_propose.review_is_placeholder(INCIDENT_LITERAL) is True


def test_review_is_placeholder_leaves_a_real_revise_alone() -> None:
    assert lifecycle_propose.review_is_placeholder(REVISE) is False


def test_review_is_placeholder_leaves_an_empty_findings_approve_alone() -> None:
    approve = {"verdict": "approve", "findings": [], "rationale": "matches the charter"}
    assert lifecycle_propose.review_is_placeholder(approve) is False


def test_review_is_placeholder_leaves_a_real_finding_about_placeholder_code_alone() -> None:
    """A finding is a match only when its detail IS the marker, never when it merely contains one."""
    real_finding = {
        "verdict": "revise",
        "findings": [{"detail": "the patch leaves a placeholder function with no test", "charter_principle": "tests", "file": "a.py"}],
        "rationale": "the handler is unimplemented",
    }
    assert lifecycle_propose.review_is_placeholder(real_finding) is False


def test_review_is_placeholder_leaves_an_adversary_approve_with_nothing_to_object_to_alone() -> None:
    """ADVERSARY_SCHEMA has no `rationale`; an empty `strongest_objection` on an
    approve is a clean approval, not a placeholder."""
    approve = {"verdict": "approve", "objections": [], "strongest_objection": ""}
    assert lifecycle_propose.review_is_placeholder(approve) is False


def test_an_abstained_reviewers_placeholder_never_reaches_the_next_build_prompt(
    cartridge, plan_response, build_response
) -> None:
    """The failure mode itself: an abstained review must not resurface in the fix loop.

    The charter reviewer placeholders twice every round; the adversary sends
    the change back once and then approves. The retry build prompt — built
    from the sanitized stand-in, not the raw second answer — must carry
    neither the word nor the empty-object rationale the incident was made of.
    """
    scripted = ScriptedRunner(
        {
            "plan": plan_response,
            "build": [build_response, rebuilt(build_response, PATCH_ANSWERED)],
            "review_charter": [INCIDENT_LITERAL, INCIDENT_LITERAL],
            "review_adversary": [ADV_OBJECTS, ADV_APPROVES],
        }
    )
    result = lifecycle_propose.run(args(adversarial(cartridge)), scripted)

    retry_prompt = roles(scripted, "build")[1]["prompt"]
    assert "placeholder" not in retry_prompt.lower()
    assert "{}" not in retry_prompt
    assert [p["kind"] for p in result["proposals"]] == ["draft_pr_create"]


def test_a_reviewer_that_placeholders_twice_abstains_and_the_other_decides_alone(
    cartridge, plan_response, build_response
) -> None:
    scripted = ScriptedRunner(
        {
            "plan": plan_response,
            "build": build_response,
            "review_charter": [INCIDENT_LITERAL, INCIDENT_LITERAL],
            "review_adversary": ADV_APPROVES,
        }
    )
    result = lifecycle_propose.run(args(adversarial(cartridge)), scripted)

    assert len(roles(scripted, "review_charter")) == 2, "asked again once, then abstained"
    assert len(roles(scripted, "arbitrate")) == 0, "one reviewer abstained; nothing to arbitrate between"
    assert result["arbitration"] is None
    assert result["fix_loop"]["review_placeholder"] is True
    assert [p["kind"] for p in result["proposals"]] == ["draft_pr_create"], "the adversary's approval stands alone"


def test_both_reviewers_placeholdering_quarantines_rather_than_rebuilds(
    cartridge, plan_response, build_response
) -> None:
    scripted = ScriptedRunner(
        {
            "plan": plan_response,
            "build": build_response,
            "review_charter": [INCIDENT_LITERAL, INCIDENT_LITERAL],
            "review_adversary": [INCIDENT_LITERAL, INCIDENT_LITERAL],
        }
    )
    result = lifecycle_propose.run(args(adversarial(cartridge)), scripted)

    assert len(roles(scripted, "build")) == 1, "quarantined, not sent back for another attempt"
    assert result["fix_loop"]["stopped"] == "harness fault: review placeholders"
    assert result["fix_loop"]["review_placeholder"] is True
    assert result["proposals"] == []
