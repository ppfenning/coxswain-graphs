"""Decomposition, and the adversary whose job is to delete dependency edges.

Every edge that is not real serialises work that could have run at once, and
the person who just drew the graph is the last person likely to spot one.
"""

from __future__ import annotations

import pytest

from graphs._contract import ContractViolation
from graphs.delivery import initiative_decompose
from runner import ScriptedRunner

DECOMPOSITION = {
    "phases": [{"id": "p1", "goal": "foundations"}, {"id": "p2", "goal": "cutover"}],
    "tasks": [
        {"id": "t1", "phase": "p1", "title": "schema probe", "body": "b", "needs": [], "surfaces": ["schema"]},
        {"id": "t2", "phase": "p1", "title": "bench harness", "body": "b", "needs": ["t1"], "surfaces": []},
        {"id": "t3", "phase": "p2", "title": "cutover", "body": "b", "needs": ["t1", "t2"], "surfaces": ["migration"]},
    ],
    "rationale": "multi-phase",
}

ACCEPTED = {"spurious_edges": [], "missing_edges": [], "verdict": "accept", "summary": "edges hold"}


@pytest.fixture
def cart(cartridge) -> dict:
    cartridge["skills"]["decompose"] = "acme-skills:decompose"
    cartridge["work_routing"] = {"states": {"active": "work", "planned": "work", "future": "backlog"}}
    cartridge["write_kinds"]["item_create"] = {"risk": "low", "ramp": "deferred"}
    cartridge["write_kinds"]["state_move"] = {"risk": "low", "ramp": "deferred"}
    return cartridge


def decompose(cart, decomposition=DECOMPOSITION, challenge=None):
    responses = {"decompose": decomposition}
    if challenge is not None:
        # The adversary only runs when the team has bound it — optional means optional.
        cart["skills"]["review_adversary"] = "acme-skills:review-adversary"
        responses["review_adversary"] = challenge
    return initiative_decompose.run(
        {"run_id": "r", "date": "2026-08-30", "cartridge": cart, "idea": "go arrow-native"},
        ScriptedRunner(responses),
    )


def test_emits_one_proposal_per_task(cart) -> None:
    result = decompose(cart)
    assert [p["target"] for p in result["proposals"]] == ["t1", "t2", "t3", "initiative"]
    assert all(p["kind"] == "item_create" for p in result["proposals"])


def test_totals_report_the_shape_of_the_graph(cart) -> None:
    totals = decompose(cart)["totals"]
    assert totals["tasks"] == 3
    assert totals["phases"] == 2
    assert totals["edges"] == 3
    assert totals["immediately_startable"] == 1


def test_a_proposal_says_what_blocks_it(cart) -> None:
    result = decompose(cart)
    unblocked = next(p for p in result["proposals"] if p["target"] == "t1")
    assert any("can start immediately" in e["output"] for e in unblocked["evidence"])


# ── the schema requests what the code reads ────────────────────────────────


def test_the_schema_actually_requests_a_goal_per_phase() -> None:
    phase_schema = initiative_decompose.DECOMPOSE_SCHEMA["properties"]["phases"]["items"]
    assert phase_schema["required"] == ["id", "goal"]


# ── initiative.md ────────────────────────────────────────────────────────────


def test_initiative_text_carries_phase_goals_in_order() -> None:
    idea = {"id": "regatta", "title": "Route sync", "budget_usd": 500, "why": "because races drift"}
    text = initiative_decompose.initiative_text(
        idea, ["p1", "p2"], {"p1": "foundations", "p2": "cutover"}, "coxswain-graphs"
    )
    assert "PHASE GOALS, each judged against ITS OWN line:\n- p1: foundations\n- p2: cutover" in text


def test_initiative_text_carries_the_intake_link_when_given_one() -> None:
    idea = {"id": "regatta", "title": "Route sync", "budget_usd": 500, "why": "because races drift"}
    text = initiative_decompose.initiative_text(
        idea, ["p1"], {"p1": "foundations"}, "coxswain-graphs", intake="intake/regatta.md"
    )
    assert "intake: intake/regatta.md" in text


def test_initiative_text_omits_intake_when_none_given() -> None:
    idea = {"id": "regatta", "title": "Route sync", "budget_usd": 500, "why": "because races drift"}
    text = initiative_decompose.initiative_text(idea, ["p1"], {"p1": "foundations"}, "coxswain-graphs")
    assert "intake:" not in text


def test_emit_writes_initiative_md_as_one_more_proposal(cart) -> None:
    result = decompose(cart)
    initiative = next(p for p in result["proposals"] if p["target"] == "initiative")
    assert "PHASE GOALS" in initiative["suggested_action"]
    assert "intake" not in initiative["suggested_action"]


def test_emit_proposes_linking_the_intake_file_as_its_own_write(cart) -> None:
    result = initiative_decompose.run(
        {
            "run_id": "r",
            "date": "2026-08-30",
            "cartridge": cart,
            "idea": "go arrow-native",
            "intake_path": "intake/regatta.md",
        },
        ScriptedRunner({"decompose": DECOMPOSITION}),
    )
    link = next(p for p in result["proposals"] if p["target"] == "intake/regatta.md")
    assert link["kind"] == "state_move"
    assert "cox route file --from-intake intake/regatta.md" in link["suggested_action"]
    assert "initiative: r" in link["suggested_action"]
    assert "intake/done/regatta.md" in link["suggested_action"]


def test_emit_proposes_no_intake_link_without_a_source(cart) -> None:
    result = decompose(cart)
    assert not any(p["kind"] == "state_move" for p in result["proposals"])


# ── ids scoped to an initiative ─────────────────────────────────────────────


def test_task_ids_and_files_are_prefixed_with_the_initiative_id(cart) -> None:
    result = initiative_decompose.run(
        {"run_id": "r", "date": "d", "cartridge": cart, "idea": "x", "initiative_id": "regatta"},
        ScriptedRunner({"decompose": DECOMPOSITION}),
    )
    assert [t["id"] for t in result["tasks"]] == ["regatta-t1", "regatta-t2", "regatta-t3"]


# ── the adversary on the DAG ───────────────────────────────────────────────


def test_a_spurious_edge_is_dropped_and_buys_parallelism(cart) -> None:
    """The whole point: t2 no longer waits on t1, so both can start at once."""
    challenge = {
        "spurious_edges": [{"task": "t2", "needs": "t1", "why_not_real": "the harness needs no schema"}],
        "missing_edges": [],
        "verdict": "revise",
        "summary": "one edge was imagined",
    }
    result = decompose(cart, challenge=challenge)
    t2 = next(t for t in result["tasks"] if t["id"] == "t2")
    assert t2["needs"] == []
    assert result["totals"]["immediately_startable"] == 2
    assert result["totals"]["edges_dropped"] == 1


def test_a_real_missing_edge_is_added(cart) -> None:
    challenge = {
        "spurious_edges": [],
        "missing_edges": [{"task": "t1", "needs": "t2", "why_real": "probe needs the harness"}],
        "verdict": "revise",
        "summary": "one edge was missed",
    }
    # t1 <- t2 and t2 <- t1 would be a cycle, so this challenge must be refused.
    with pytest.raises(ContractViolation, match="dependency cycle"):
        decompose(cart, challenge=challenge)


def test_the_adversary_cannot_invent_a_task(cart) -> None:
    challenge = {
        "spurious_edges": [],
        "missing_edges": [{"task": "t1", "needs": "t99-imaginary", "why_real": "made up"}],
        "verdict": "revise",
        "summary": "s",
    }
    result = decompose(cart, challenge=challenge)
    assert next(t for t in result["tasks"] if t["id"] == "t1")["needs"] == []


def test_the_adversary_cannot_stall_a_task_on_itself(cart) -> None:
    challenge = {
        "spurious_edges": [],
        "missing_edges": [{"task": "t1", "needs": "t1", "why_real": "nonsense"}],
        "verdict": "revise",
        "summary": "s",
    }
    assert next(t for t in decompose(cart, challenge=challenge)["tasks"] if t["id"] == "t1")["needs"] == []


def test_the_challenge_is_attached_as_evidence(cart) -> None:
    result = decompose(cart, challenge=ACCEPTED)
    assert any(e["check"] == "adversary on the DAG" for e in result["proposals"][0]["evidence"])


def test_adversary_edges_are_applied_even_when_args_carry_assume(cart) -> None:
    """A struck edge is absent from the filed tasks no matter what `assume` says."""
    challenge = {
        "spurious_edges": [{"task": "t2", "needs": "t1", "why_not_real": "no schema dependency"}],
        "missing_edges": [],
        "verdict": "revise",
        "summary": "one edge was imagined",
    }
    cart["skills"]["review_adversary"] = "acme-skills:review-adversary"
    result = initiative_decompose.run(
        {"run_id": "r", "date": "d", "cartridge": cart, "idea": "x", "assume": "a"},
        ScriptedRunner({"decompose": DECOMPOSITION, "review_adversary": challenge}),
    )
    filed = next(t for t in result["tasks"] if t["id"] == "t2")
    assert "t1" not in filed["needs"]


def test_without_an_adversary_the_edges_stand_unchallenged(cart) -> None:
    result = decompose(cart)
    assert result["challenge"] is None
    assert result["totals"]["edges"] == 3


# ── refusing to emit nonsense ──────────────────────────────────────────────


def test_a_cycle_from_the_decomposer_is_refused(cart) -> None:
    cyclic = {
        **DECOMPOSITION,
        "tasks": [
            {"id": "a", "phase": "p1", "title": "a", "body": "b", "needs": ["b"], "surfaces": []},
            {"id": "b", "phase": "p1", "title": "b", "body": "b", "needs": ["a"], "surfaces": []},
        ],
    }
    with pytest.raises(ContractViolation, match="could ever become ready"):
        decompose(cart, decomposition=cyclic)


def test_an_empty_decomposition_is_refused(cart) -> None:
    with pytest.raises(ContractViolation, match="no tasks"):
        decompose(cart, decomposition={**DECOMPOSITION, "tasks": []})


def test_a_team_without_the_decompose_role_is_told_so(cartridge) -> None:
    with pytest.raises(ContractViolation, match="needs the optional role 'decompose'"):
        initiative_decompose.run(
            {"run_id": "r", "date": "d", "cartridge": cartridge, "idea": "x"}, ScriptedRunner({})
        )


def test_it_refuses_without_a_cartridge(cart) -> None:
    with pytest.raises(ContractViolation, match="cartridge"):
        initiative_decompose.run({"run_id": "r", "date": "d", "idea": "x"}, ScriptedRunner({}))


def test_a_proposal_names_the_bound_landing_not_the_abstract_one(cart) -> None:
    """The first live run said `planned_work/...` and the arm refused to invent
    that directory. The routing's abstract name resolves through landing_areas."""
    bound = dict(cart, landing_areas={**(cart.get("landing_areas") or {}), "planned_work": "work"})
    result = initiative_decompose.run(
        {"run_id": "r", "date": "2026-01-01", "cartridge": bound, "idea": "x", "initiative_id": "init"},
        ScriptedRunner({"decompose": DECOMPOSITION}),
    )
    action = result["proposals"][0]["suggested_action"]
    assert action.startswith("create work/init/"), action
    assert "planned_work" not in action
    assert "title=" in action and "needs=[" in action
    empty = next(p["suggested_action"] for p in result["proposals"] if "needs=[]" in p["suggested_action"])
    assert "none" not in empty, "an empty list prints as [], never as a word the arm would copy"


# ── surfaces resolve to real paths ──────────────────────────────────────────


def test_surface_problem_of_no_unresolved_surfaces_is_none() -> None:
    assert initiative_decompose.surface_problem([]) is None


def test_surface_problem_of_one_unresolved_surface_names_it() -> None:
    assert initiative_decompose.surface_problem(["x"]) == "unbuildable: surfaces are prose — x"


def test_surface_problem_is_one_line_per_unresolved_surface_not_comma_joined() -> None:
    problem = initiative_decompose.surface_problem(["t1: foo", "t1: bar"])
    assert problem == "unbuildable: surfaces are prose — t1: foo\nunbuildable: surfaces are prose — t1: bar"


def test_apply_surface_resolutions_does_not_mutate_its_argument() -> None:
    tasks = [{"id": "t1", "surfaces": ["schema"]}]
    tree = [{"repo": "graphs", "path": "graphs/schema.py"}]
    original = tasks[0]

    resolved = initiative_decompose._apply_surface_resolutions(tasks, tree)

    assert tasks[0] is original
    assert tasks[0]["surfaces"] == ["schema"]
    assert resolved[0]["surfaces"] == ["graphs/schema.py"]


def test_a_tree_with_unresolved_surfaces_and_no_adversary_quarantines_the_run(cart) -> None:
    decomposition = {
        **DECOMPOSITION,
        "tasks": [{"id": "t1", "phase": "p1", "title": "a", "body": "b", "needs": [], "surfaces": ["foo", "bar"]}],
    }
    runner = ScriptedRunner({"decompose": decomposition})
    with pytest.raises(ContractViolation, match="unbuildable") as exc:
        initiative_decompose.run(
            {"run_id": "r", "date": "d", "cartridge": cart, "idea": "x", "tree": [{"repo": "g", "path": "g/other.py"}]},
            runner,
        )
    message = str(exc.value)
    assert "t1: foo" in message
    assert "t1: bar" in message
    assert message.count("\n") == 1, "one line per unresolved surface, not one comma-joined clause per task"


def test_the_adversary_gets_an_unbuildable_challenge_naming_task_and_prose(cart) -> None:
    tree = [{"repo": "graphs", "path": "graphs/schema.py"}]
    decomposition = {
        **DECOMPOSITION,
        "tasks": [{"id": "t1", "phase": "p1", "title": "a", "body": "b", "needs": [], "surfaces": ["widget-thing"]}],
    }
    correction = {
        "corrections": [{"task": "t1", "surface": "widget-thing", "replacement": "schema.py"}],
        "summary": "resolved",
    }
    cart["skills"]["review_adversary"] = "acme-skills:review-adversary"
    runner = ScriptedRunner({"decompose": decomposition, "review_adversary": [ACCEPTED, correction]})
    result = initiative_decompose.run(
        {"run_id": "r", "date": "d", "cartridge": cart, "idea": "x", "tree": tree}, runner
    )
    unbuildable_call = runner.calls[-1]
    assert unbuildable_call["role"] == "review_adversary"
    assert "t1: widget-thing" in unbuildable_call["prompt"]
    assert result["tasks"][0]["surfaces"] == ["graphs/schema.py"]


def test_a_second_unresolved_set_after_the_adversarys_attempt_quarantines_the_run(cart) -> None:
    decomposition = {
        **DECOMPOSITION,
        "tasks": [{"id": "t1", "phase": "p1", "title": "a", "body": "b", "needs": [], "surfaces": ["widget-thing"]}],
    }
    bad_correction = {
        "corrections": [{"task": "t1", "surface": "widget-thing", "replacement": "still-prose"}],
        "summary": "tried",
    }
    cart["skills"]["review_adversary"] = "acme-skills:review-adversary"
    runner = ScriptedRunner({"decompose": decomposition, "review_adversary": [ACCEPTED, bad_correction]})
    with pytest.raises(ContractViolation, match="unbuildable"):
        initiative_decompose.run(
            {"run_id": "r", "date": "d", "cartridge": cart, "idea": "x", "tree": [{"repo": "g", "path": "g/other.py"}]},
            runner,
        )


def test_a_task_whose_surfaces_span_two_repos_is_split_one_per_repo(cart) -> None:
    tree = [
        {"repo": "graphs-repo", "path": "graphs/schema.py"},
        {"repo": "harness-repo", "path": "harness/epic.py"},
    ]
    decomposition = {
        **DECOMPOSITION,
        "tasks": [
            {"id": "t1", "phase": "p1", "title": "a", "body": "b", "needs": [], "surfaces": ["schema.py", "epic.py"]},
            {"id": "t2", "phase": "p1", "title": "b", "body": "b", "needs": ["t1"], "surfaces": []},
        ],
    }
    runner = ScriptedRunner({"decompose": decomposition})
    result = initiative_decompose.run(
        {"run_id": "r", "date": "d", "cartridge": cart, "idea": "x", "tree": tree}, runner
    )
    ids = sorted(t["id"] for t in result["tasks"])
    assert ids == ["t1--graphs-repo", "t1--harness-repo", "t2"]
    graphs_task = next(t for t in result["tasks"] if t["id"] == "t1--graphs-repo")
    assert graphs_task["surfaces"] == ["graphs/schema.py"]
    t2 = next(t for t in result["tasks"] if t["id"] == "t2")
    assert sorted(t2["needs"]) == ["t1--graphs-repo", "t1--harness-repo"]


def test_without_a_tree_surfaces_stay_as_declared(cart) -> None:
    """No tree means nothing to resolve against; the old behaviour is unchanged."""
    result = decompose(cart)
    t1 = next(t for t in result["tasks"] if t["id"] == "t1")
    assert t1["surfaces"] == ["schema"]
