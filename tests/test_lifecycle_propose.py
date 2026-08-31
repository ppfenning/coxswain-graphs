"""lifecycle-propose: proposes, never writes; and never invents a risk."""

from __future__ import annotations

import pytest

from graphs import lifecycle_propose
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
    assert set(result) == {"run_id", "date", "ticket", "plan", "build", "review", "change_facts", "proposals"}


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
