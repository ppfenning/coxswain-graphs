"""Decomposition, and the adversary whose job is to delete dependency edges.

Every edge that is not real serialises work that could have run at once, and
the person who just drew the graph is the last person likely to spot one.
"""

from __future__ import annotations

import json

import pytest
import yaml

from graphs._contract import ContractViolation
from graphs.delivery import initiative_decompose
from runner import ScriptedRunner
from runner.decision_log import RouterDecision
from runner.tier_resolution import Hints

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


def test_a_same_phase_edge_is_split_and_the_move_is_recorded(cart) -> None:
    stub = {
        "phases": [{"id": "p1", "goal": "g1"}, {"id": "p2", "goal": "g2"}],
        "tasks": [
            {"id": "a", "phase": "p1", "title": "a", "body": "b", "needs": [], "surfaces": []},
            {"id": "b", "phase": "p1", "title": "b", "body": "b", "needs": ["a"], "surfaces": []},
        ],
        "rationale": "r",
    }
    result = decompose(cart, stub)
    phase_of = {t["id"]: t["phase"] for t in result["tasks"]}
    assert all(phase_of[n] != t["phase"] for t in result["tasks"] for n in t["needs"])
    assert result["moves"] == [{"task": "b", "from": "p1", "to": "p1-b", "needs": ["a"]}]
    assert [p["id"] for p in result["phases"]] == ["p1", "p1-b", "p2"]


def test_a_decomposition_with_no_same_phase_edge_records_no_moves(cart) -> None:
    stub = {
        "phases": [{"id": "p1", "goal": "g1"}, {"id": "p2", "goal": "g2"}],
        "tasks": [
            {"id": "a", "phase": "p1", "title": "a", "body": "b", "needs": [], "surfaces": []},
            {"id": "b", "phase": "p2", "title": "b", "body": "b", "needs": ["a"], "surfaces": []},
        ],
        "rationale": "r",
    }
    result = decompose(cart, stub)
    assert result["moves"] == []
    assert {t["id"]: t["phase"] for t in result["tasks"]} == {"a": "p1", "b": "p2"}


def test_emits_one_proposal_per_task(cart) -> None:
    result = decompose(cart)
    assert [p["target"] for p in result["proposals"]] == ["t1", "t2", "t3", "initiative"]
    assert all(p["kind"] == "item_create" for p in result["proposals"])


def test_totals_report_the_shape_of_the_graph(cart) -> None:
    totals = decompose(cart)["totals"]
    assert totals["tasks"] == 3
    assert totals["phases"] == 3
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


def _frontmatter(title: str) -> dict:
    idea = {"id": "regatta", "title": title, "budget_usd": 500, "why": "because races drift"}
    text = initiative_decompose.initiative_text(idea, ["p1"], {"p1": "foundations"}, "coxswain-graphs")
    return yaml.safe_load(text.split("---\n")[1])


def test_a_title_starting_with_a_backtick_round_trips_through_yaml() -> None:
    assert _frontmatter("`tick` the cache")["title"] == "`tick` the cache"


def test_a_plain_title_renders_unquoted() -> None:
    idea = {"id": "regatta", "title": "Route sync", "budget_usd": 500, "why": "because races drift"}
    text = initiative_decompose.initiative_text(idea, ["p1"], {"p1": "foundations"}, "coxswain-graphs")
    assert "\ntitle: Route sync\n" in text


def test_a_title_with_a_colon_and_space_round_trips_through_yaml() -> None:
    assert _frontmatter("Fix parser: short rows")["title"] == "Fix parser: short rows"


def test_a_non_ascii_title_renders_unquoted_and_unescaped() -> None:
    idea = {"id": "regatta", "title": "Café sync — phase 2", "budget_usd": 500, "why": "because races drift"}
    text = initiative_decompose.initiative_text(idea, ["p1"], {"p1": "foundations"}, "coxswain-graphs")
    assert "\ntitle: Café sync — phase 2\n" in text


def test_id_repo_budget_and_intake_round_trip_through_yaml_with_no_document_marker() -> None:
    idea = {"id": "2026", "title": "Route sync", "budget_usd": 12.5, "why": "because races drift"}
    text = initiative_decompose.initiative_text(
        idea, ["p1"], {"p1": "foundations"}, "coxswain-graphs", intake="intake/regatta.md"
    )
    assert (yaml.safe_load(text.split("---\n")[1]), "..." in text) == (
        {
            "id": "2026",
            "title": "Route sync",
            "repo": "coxswain-graphs",
            "budget_usd": 12.5,
            "intake": "intake/regatta.md",
        },
        False,
    )


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
    assert result["moves"] == [{"task": "regatta-t2", "from": "p1", "to": "p1-b", "needs": ["regatta-t1"]}]


def test_ids_and_needs_the_model_already_prefixed_stay_single_prefixed(cart) -> None:
    answer = dict(
        DECOMPOSITION,
        tasks=[
            dict(t, id=f"regatta-{t['id']}", needs=[f"regatta-{n}" for n in t["needs"]])
            for t in DECOMPOSITION["tasks"]
        ],
    )
    result = initiative_decompose.run(
        {"run_id": "r", "date": "d", "cartridge": cart, "idea": "x", "initiative_id": "regatta"},
        ScriptedRunner({"decompose": answer}),
    )
    assert [t["id"] for t in result["tasks"]] == ["regatta-t1", "regatta-t2", "regatta-t3"]
    assert next(t for t in result["tasks"] if t["id"] == "regatta-t3")["needs"] == ["regatta-t1", "regatta-t2"]


# ── ordinal task_ids mode ───────────────────────────────────────────────────


def test_ordinal_task_ids_map_through_prefixed_in_listed_order(cart) -> None:
    result = initiative_decompose.run(
        {
            "run_id": "r",
            "date": "d",
            "cartridge": cart,
            "idea": "x",
            "initiative_id": "I412",
            "task_ids": "ordinal",
        },
        ScriptedRunner({"decompose": DECOMPOSITION}),
    )
    assert [t["id"] for t in result["tasks"]] == ["I412-t1", "I412-t2", "I412-t3"]
    assert next(t for t in result["tasks"] if t["id"] == "I412-t2")["needs"] == ["I412-t1"]


def test_an_ordinal_needs_key_naming_no_listed_task_fails_as_invalid_output(cart) -> None:
    bad = dict(
        DECOMPOSITION,
        tasks=[
            dict(DECOMPOSITION["tasks"][0]),
            dict(DECOMPOSITION["tasks"][1], needs=["t9"]),
            dict(DECOMPOSITION["tasks"][2]),
        ],
    )
    with pytest.raises(ContractViolation):
        initiative_decompose.run(
            {
                "run_id": "r",
                "date": "d",
                "cartridge": cart,
                "idea": "x",
                "initiative_id": "I412",
                "task_ids": "ordinal",
            },
            ScriptedRunner({"decompose": bad}),
        )


def test_default_slug_mode_applies_no_ordinal_validation_but_still_drops_a_foreign_need(cart) -> None:
    """Ordinal mode would fail a `needs` key naming no listed task; slug mode does not — but the
    plan-level needs validation still drops it once no task in the finished plan carries that id."""
    dangling = {
        "phases": [{"id": "p1", "goal": "foundations"}],
        "tasks": [
            {
                "id": "schema-probe",
                "phase": "p1",
                "title": "schema probe",
                "body": "b",
                "needs": ["phantom"],
                "surfaces": [],
            },
        ],
        "rationale": "slug",
    }
    args = {"run_id": "r", "date": "d", "cartridge": cart, "idea": "x", "initiative_id": "I412"}
    for task_ids_arg in ({}, {"task_ids": "slug"}):
        result = initiative_decompose.run(dict(args, **task_ids_arg), ScriptedRunner({"decompose": dangling}))
        task = result["tasks"][0]
        assert task["id"] == "I412-schema-probe"
        assert task["needs"] == []
        assert task["needs_dropped"] == ["I412-phantom"]


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


# ── phase validation and unexecuted item_create ─────────────────────────────


def test_a_near_miss_phase_is_corrected_and_the_evidence_names_it(cart) -> None:
    near_miss = {
        "phases": [{"id": "faulthandler-signal", "goal": "signal handling"}],
        "tasks": [
            {
                "id": "t1",
                "phase": "faulthandler-signl",
                "title": "dump stacks",
                "body": "b",
                "needs": [],
                "surfaces": [],
            },
        ],
        "rationale": "single task",
    }
    result = decompose(cart, decomposition=near_miss)
    task = next(t for t in result["tasks"] if t["id"] == "t1")
    assert task["phase"] == "faulthandler-signal"
    t1_proposal = next(p for p in result["proposals"] if p["target"] == "t1")
    assert {"check": "phase correction", "output": 'phase "faulthandler-signl" corrected to "faulthandler-signal"'} in (
        t1_proposal["evidence"]
    )


def test_a_phase_too_far_from_its_declared_phase_fails_the_node(cart) -> None:
    too_far = {
        "phases": [{"id": "faulthandler-signal", "goal": "signal handling"}],
        "tasks": [
            {
                "id": "t1",
                "phase": "faulthandring-signal",
                "title": "dump stacks",
                "body": "b",
                "needs": [],
                "surfaces": [],
            },
        ],
        "rationale": "single task",
    }
    with pytest.raises(ContractViolation) as excinfo:
        decompose(cart, decomposition=too_far)
    assert "t1" in str(excinfo.value)
    assert "faulthandring-signal" in str(excinfo.value)


def test_a_phase_equidistant_from_two_declared_phases_fails_the_node(cart) -> None:
    ambiguous = {
        "phases": [{"id": "pax", "goal": "a"}, {"id": "pbx", "goal": "b"}],
        "tasks": [
            {"id": "t1", "phase": "p1x", "title": "x", "body": "b", "needs": [], "surfaces": []},
        ],
        "rationale": "ambiguous phase",
    }
    with pytest.raises(ContractViolation) as excinfo:
        decompose(cart, decomposition=ambiguous)
    assert "t1" in str(excinfo.value)
    assert "p1x" in str(excinfo.value)


def test_an_approved_unapplied_item_create_diff_returns_its_refusal_reason() -> None:
    diffs = [
        {"kind": "item_create", "target": "t1", "decision": "approved", "risk": "low", "outcome": "skipped", "applied": False, "edited": False},
    ]
    assert initiative_decompose.unexecuted_item_create_reason(diffs) == "skipped"


def test_an_applied_item_create_diff_leaves_the_run_unflagged() -> None:
    diffs = [
        {"kind": "item_create", "target": "t1", "decision": "approved", "risk": "low", "outcome": "clean", "applied": True, "edited": False},
    ]
    assert initiative_decompose.unexecuted_item_create_reason(diffs) is None


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
    assert "title:" in action and "needs:" in action
    empty = next(p["suggested_action"] for p in result["proposals"] if "needs: []" in p["suggested_action"])
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


def test_a_misplaced_test_surface_is_corrected_to_the_real_tests_path(cart) -> None:
    decomposition = {
        **DECOMPOSITION,
        "tasks": [
            {
                "id": "t1",
                "phase": "p1",
                "title": "a",
                "body": "b",
                "needs": [],
                "surfaces": ["agent_tools/tests/test_route.py"],
            }
        ],
    }
    runner = ScriptedRunner({"decompose": decomposition})
    result = initiative_decompose.run(
        {"run_id": "r", "date": "d", "cartridge": cart, "idea": "x", "tree": [{"repo": "g", "path": "tests/test_route.py"}]},
        runner,
    )
    task = result["tasks"][0]
    assert task["surfaces"] == ["tests/test_route.py"]
    proposal = next(p for p in result["proposals"] if p["target"] == "t1")
    assert {
        "check": "surface corrected",
        "output": "agent_tools/tests/test_route.py -> tests/test_route.py",
    } in proposal["evidence"]


def test_a_missing_non_test_surface_fails_the_node_as_invalid_output(cart) -> None:
    decomposition = {
        **DECOMPOSITION,
        "tasks": [{"id": "t1", "phase": "p1", "title": "a", "body": "b", "needs": [], "surfaces": ["graphs/nope.py"]}],
    }
    runner = ScriptedRunner({"decompose": decomposition})
    with pytest.raises(ContractViolation, match="graphs/nope.py"):
        initiative_decompose.run(
            {"run_id": "r", "date": "d", "cartridge": cart, "idea": "x", "tree": [{"repo": "g", "path": "g/other.py"}]},
            runner,
        )


def test_a_needs_entry_naming_a_task_absent_from_the_plan_is_dropped(cart) -> None:
    decomposition = {
        **DECOMPOSITION,
        "tasks": [
            {"id": "t1", "phase": "p1", "title": "a", "body": "b", "needs": [], "surfaces": []},
            {"id": "t2", "phase": "p1", "title": "b", "body": "b", "needs": ["t1", "ghost"], "surfaces": []},
        ],
    }
    result = decompose(cart, decomposition)
    t2 = next(t for t in result["tasks"] if t["id"] == "t2")
    assert t2["needs"] == ["t1"]
    proposal = next(p for p in result["proposals"] if p["target"] == "t2")
    assert {
        "check": "needs dropped",
        "output": "t2: dropped 'ghost', not a task id in this plan",
    } in proposal["evidence"]


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


def test_without_a_tree_only_path_shaped_surfaces_stay_declared(cart) -> None:
    """No tree means nothing to resolve against, but a bare declared token still isn't a path."""
    result = decompose(cart)
    t1 = next(t for t in result["tasks"] if t["id"] == "t1")
    assert t1["surfaces"] == []
    assert t1["lint"] == ["dropped from surfaces: schema"]


def test_a_reach_problem_comes_back_through_apply_corrections_as_a_refusal(cart) -> None:
    decomposition = {
        **DECOMPOSITION,
        "tasks": [{"id": "t1", "phase": "p1", "title": "a", "body": "b", "needs": [], "surfaces": ["~/scratch/notes.md"]}],
    }
    correction = {
        "corrections": [{"task": "t1", "surface": "~/scratch/notes.md", "replacement": "graphs/notes.py"}],
        "summary": "resolved",
    }
    cart["skills"]["review_adversary"] = "acme-skills:review-adversary"
    runner = ScriptedRunner({"decompose": decomposition, "review_adversary": [ACCEPTED, correction]})
    result = initiative_decompose.run({"run_id": "r", "date": "d", "cartridge": cart, "idea": "x"}, runner)
    lint_call = runner.calls[-1]
    assert lint_call["role"] == "review_adversary"
    assert "t1: reach" in lint_call["prompt"]
    assert result["tasks"][0]["surfaces"] == ["graphs/notes.py"]


def test_a_body_sourced_reach_problem_still_quarantines_even_with_a_correction(cart) -> None:
    """The correction schema only rewrites `surfaces`; a path named in prose has nothing to match."""
    decomposition = {
        **DECOMPOSITION,
        "tasks": [{"id": "t1", "phase": "p1", "title": "a", "body": "notes live at ~/scratch/notes.md", "needs": [], "surfaces": []}],
    }
    correction = {
        "corrections": [{"task": "t1", "surface": "~/scratch/notes.md", "replacement": "graphs/notes.py"}],
        "summary": "resolved",
    }
    cart["skills"]["review_adversary"] = "acme-skills:review-adversary"
    runner = ScriptedRunner({"decompose": decomposition, "review_adversary": [ACCEPTED, correction]})
    with pytest.raises(ContractViolation, match="reach"):
        initiative_decompose.run({"run_id": "r", "date": "d", "cartridge": cart, "idea": "x"}, runner)


def test_a_coupling_problem_comes_back_through_apply_corrections_as_a_refusal(cart) -> None:
    decomposition = {
        **DECOMPOSITION,
        "tasks": [
            {"id": "t1", "phase": "p1", "title": "a", "body": "b", "needs": [], "surfaces": ["tests/test_x.py"]},
            {"id": "t2", "phase": "p1", "title": "b", "body": "b", "needs": [], "surfaces": ["tests/test_x.py"]},
        ],
    }
    correction = {
        "corrections": [{"task": "t2", "surface": "tests/test_x.py", "replacement": "tests/test_y.py"}],
        "summary": "split",
    }
    cart["skills"]["review_adversary"] = "acme-skills:review-adversary"
    runner = ScriptedRunner({"decompose": decomposition, "review_adversary": [ACCEPTED, correction]})
    result = initiative_decompose.run({"run_id": "r", "date": "d", "cartridge": cart, "idea": "x"}, runner)
    lint_call = runner.calls[-1]
    assert lint_call["role"] == "review_adversary"
    assert "coupling" in lint_call["prompt"]
    surfaces = {t["id"]: t["surfaces"] for t in result["tasks"]}
    assert surfaces["t2"] == ["tests/test_y.py"]


def _one_ticket(body: str, surfaces: list[str] | None = None) -> dict:
    task = {"id": "t1", "phase": "p1", "title": "a", "body": body, "needs": [], "surfaces": surfaces or []}
    return {**DECOMPOSITION, "tasks": [task]}


RUFF_BODY = "```bash\nruff check .\n```"
PYTEST_BODY = "```bash\npytest -q\n```"
GRANT_ARGS = {"run_id": "r", "date": "d", "idea": "x", "grants": ["pytest"]}


def test_a_grant_refusal_is_rewritten_by_decompose_and_the_clean_rewrite_is_stored(cart) -> None:
    runner = ScriptedRunner({"decompose": [_one_ticket(RUFF_BODY), _one_ticket(PYTEST_BODY)]})
    result = initiative_decompose.run({**GRANT_ARGS, "cartridge": cart}, runner)
    rewrite_call = runner.calls[-1]
    assert [c["role"] for c in runner.calls] == ["decompose", "decompose"]
    assert "t1: grant" in rewrite_call["prompt"]
    assert "pytest" in rewrite_call["prompt"]
    task = next(t for t in result["tasks"] if t["id"] == "t1")
    assert task["body"] == PYTEST_BODY
    assert not task.get("lint")
    assert any(p["target"] == "t1" for p in result["proposals"])


def test_a_cross_repo_refusal_is_rewritten_by_decompose_and_the_clean_rewrite_is_stored(cart) -> None:
    runner = ScriptedRunner(
        {"decompose": [_one_ticket("b", ["harness/cli.py"]), _one_ticket("b", ["agent_tools/dash.py (new)"])]}
    )
    result = initiative_decompose.run({**GRANT_ARGS, "cartridge": cart, "repo": "coxswain-tools"}, runner)
    assert [c["role"] for c in runner.calls] == ["decompose", "decompose"]
    assert "t1: cross_repo" in runner.calls[-1]["prompt"]
    task = next(t for t in result["tasks"] if t["id"] == "t1")
    assert task["surfaces"] == ["agent_tools/dash.py (new)"]
    assert not any("cross_repo" in entry for entry in task.get("lint") or [])


def test_a_grant_refusal_that_survives_the_rewrite_stores_nothing_and_names_ticket_and_rule(cart) -> None:
    runner = ScriptedRunner({"decompose": [_one_ticket(RUFF_BODY), _one_ticket(RUFF_BODY)]})
    with pytest.raises(initiative_decompose.LintRefusal) as caught:
        initiative_decompose.run({**GRANT_ARGS, "cartridge": cart}, runner)
    assert str(caught.value).splitlines()[0] == "lint-refused: t1 grant"
    assert len(runner.calls) == 2


def test_a_grant_and_a_reach_hit_go_to_the_rewrite_then_the_adversary_correction(cart) -> None:
    correction = {
        "corrections": [{"task": "t1", "surface": "~/scratch/notes.md", "replacement": "graphs/notes.py"}],
        "summary": "resolved",
    }
    cart["skills"]["review_adversary"] = "acme-skills:review-adversary"
    runner = ScriptedRunner(
        {
            "decompose": [_one_ticket(RUFF_BODY, ["~/scratch/notes.md"]), _one_ticket(PYTEST_BODY, ["~/scratch/notes.md"])],
            "review_adversary": [ACCEPTED, ACCEPTED, correction],
        }
    )
    result = initiative_decompose.run({**GRANT_ARGS, "cartridge": cart}, runner)
    assert [c["role"] for c in runner.calls] == [
        "decompose", "review_adversary", "decompose", "review_adversary", "review_adversary",
    ]
    assert "t1: reach" in runner.calls[-1]["prompt"]
    task = next(t for t in result["tasks"] if t["id"] == "t1")
    assert (task["body"], task["surfaces"]) == (PYTEST_BODY, ["graphs/notes.py"])


def test_a_single_ticket_with_a_grant_hit_is_refused_without_a_decompose_rewrite(cart) -> None:
    intake = {**INTAKE, "body": RUFF_BODY}
    with pytest.raises(initiative_decompose.LintRefusal) as caught:
        _sized(cart, {"size": TASK_VERDICT}, intake=intake)
    assert str(caught.value).splitlines()[0] == "lint-refused: fix-parser grant"


def test_the_refusal_reason_names_each_refused_ticket_and_rule_once() -> None:
    problems = [
        initiative_decompose.Problem("t1", "grant", "names `ruff`, which is not granted", "fix"),
        initiative_decompose.Problem("t1", "grant", "names `cox`, which is not granted", "fix"),
        initiative_decompose.Problem("t2", "cross_repo", "names harness/cli.py, which lives in graphs", "fix"),
    ]
    first = initiative_decompose._refusal_reason(problems).splitlines()[0]
    assert first == "lint-refused: t1 grant, t2 cross_repo"


def test_a_size_problem_is_recorded_as_a_lint_entry_not_a_refusal(cart) -> None:
    body = " ".join(["word"] * 750)
    decomposition = {
        **DECOMPOSITION,
        "tasks": [{"id": "t1", "phase": "p1", "title": "a", "body": body, "needs": [], "surfaces": []}],
    }
    result = decompose(cart, decomposition)
    lint_entry = "size: body is 750 words (point at a spec file in the repository)"
    task = next(t for t in result["tasks"] if t["id"] == "t1")
    assert task["lint"] == [lint_entry]
    ticket = next(p for p in result["proposals"] if p["target"] == "t1")
    assert f"lint:\n- '{lint_entry}'" in ticket["suggested_action"]
    assert {"check": "lint", "output": lint_entry} in ticket["evidence"]


def test_a_size_advisory_lands_under_lint_and_never_in_surfaces(cart) -> None:
    runner = ScriptedRunner({"decompose": [_one_ticket(" ".join(["word"] * 750), ["graphs/schema.py"])]})
    result = initiative_decompose.run({**GRANT_ARGS, "cartridge": cart}, runner)
    lint_entry = "size: body is 750 words (point at a spec file in the repository)"
    task = next(t for t in result["tasks"] if t["id"] == "t1")
    assert len(runner.calls) == 1
    assert task["lint"] == [lint_entry]
    assert task["surfaces"] == ["graphs/schema.py"]


def test_a_clean_plan_is_stored_on_the_first_output(cart) -> None:
    runner = ScriptedRunner({"decompose": [_one_ticket(PYTEST_BODY)]})
    result = initiative_decompose.run({**GRANT_ARGS, "cartridge": cart}, runner)
    assert [c["role"] for c in runner.calls] == ["decompose"]
    assert any(p["target"] == "t1" for p in result["proposals"])


def test_a_non_path_entry_in_surfaces_is_dropped_to_lint(cart) -> None:
    advisory = "grant: names `cox`, which is not granted (name only pytest, git status, git diff)"
    decomposition = {
        **DECOMPOSITION,
        "tasks": [
            {
                "id": "t1",
                "phase": "p1",
                "title": "a",
                "body": "b",
                "needs": [],
                "surfaces": ["graphs/schema.py", "pkg/mod.py (new)", "widget-thing", advisory],
            },
        ],
    }
    result = decompose(cart, decomposition)
    task = next(t for t in result["tasks"] if t["id"] == "t1")
    assert task["surfaces"] == ["graphs/schema.py", "pkg/mod.py (new)"]
    assert task["lint"] == ["dropped from surfaces: widget-thing", f"dropped from surfaces: {advisory}"]


def _decompose_for(cart, repo: str, surfaces: list[str]) -> dict:
    decomposition = {
        **DECOMPOSITION,
        "tasks": [{"id": "t1", "phase": "p1", "title": "a", "body": "b", "needs": [], "surfaces": surfaces}],
    }
    return initiative_decompose.run(
        {"run_id": "r", "date": "2026-08-30", "cartridge": cart, "idea": "x", "repo": repo},
        ScriptedRunner({"decompose": decomposition}),
    )


def test_a_graphs_ticket_naming_its_own_new_files_carries_no_cross_repo_lint(cart) -> None:
    result = _decompose_for(cart, "graphs", ["graphs/schema.py", "harness/new_module.py"])
    task = next(t for t in result["tasks"] if t["id"] == "t1")
    assert "lint" not in task or not any(entry.startswith("cross_repo") for entry in task["lint"])


def test_a_tools_ticket_naming_a_graphs_file_is_refused_with_the_cross_repo_reason(cart) -> None:
    with pytest.raises(initiative_decompose.LintRefusal) as caught:
        _decompose_for(cart, "coxswain-tools", ["harness/cli.py"])
    assert str(caught.value).splitlines() == [
        "lint-refused: t1 cross_repo",
        "t1: cross_repo — names harness/cli.py, which lives in graphs "
        "(paste the code the build needs into the ticket: a build reads only its own repository)",
    ]


def test_a_title_carrying_a_colon_round_trips_through_yaml(cart) -> None:
    title = "Pure steward core: evidence-bar check and proposal rendering for the ceiling case"
    decomposition = {
        **DECOMPOSITION,
        "tasks": [{"id": "t1", "phase": "p1", "title": title, "body": "b", "needs": [], "surfaces": []}],
    }
    result = decompose(cart, decomposition)
    ticket = next(p for p in result["proposals"] if p["target"] == "t1")
    block = ticket["suggested_action"].split("with frontmatter\n", 1)[1].rsplit("\nbody = ", 1)[0]
    assert yaml.safe_load(block)["title"] == title


def test_a_body_ending_in_a_closing_tag_is_written_without_it(cart) -> None:
    decomposition = {
        **DECOMPOSITION,
        "tasks": [{"id": "t1", "phase": "p1", "title": "a", "body": "the rationale text\n</content>", "needs": [], "surfaces": []}],
    }
    result = decompose(cart, decomposition)
    ticket = next(p for p in result["proposals"] if p["target"] == "t1")
    assert ticket["rationale"] == "the rationale text"


def test_duplicated_lint_entries_are_written_once(cart) -> None:
    result = decompose(cart, _one_ticket(PYTEST_BODY))
    lint_entry = "size: body is 750 words (point at a spec file in the repository)"
    task = dict(next(t for t in result["tasks"] if t["id"] == "t1"), lint=[lint_entry, lint_entry, lint_entry])
    action = initiative_decompose._item_action(task, landing="work", initiative_id=None)
    block = action.split("with frontmatter\n", 1)[1].rsplit("\nbody = ", 1)[0]
    assert yaml.safe_load(block)["lint"] == [lint_entry]


# ── tier routing: decompose declares hints, the adversary names its tier ─────


def test_the_decompose_call_declares_hints_and_no_tier(cart) -> None:
    cart["skills"]["review_adversary"] = "acme-skills:review-adversary"
    runner = ScriptedRunner({"decompose": DECOMPOSITION, "review_adversary": ACCEPTED})
    initiative_decompose.run({"run_id": "r", "date": "d", "cartridge": cart, "idea": "x"}, runner)
    call = next(c for c in runner.calls if c["role"] == "decompose")
    assert call["tier"] is None
    assert call["hints"] == Hints(judgment="high")


def test_every_adversary_call_keeps_the_deep_literal(cart) -> None:
    """Lifecycle's review_adversary is `standard`; decompose's stays `deep` by naming it."""
    cart["skills"]["review_adversary"] = "acme-skills:review-adversary"
    surface = {"task": "t1", "surface": "widget-thing", "replacement": "schema.py"}
    reach = {"task": "t1", "surface": "~/scratch/notes.md", "replacement": "graphs/notes.py"}
    scenarios = [
        ("edge and unbuildable", ["widget-thing"], [{"repo": "graphs", "path": "graphs/schema.py"}], surface),
        ("edge and lint refusal", ["~/scratch/notes.md"], [], reach),
    ]
    for name, surfaces, tree, correction in scenarios:
        tasks = [{"id": "t1", "phase": "p1", "title": "a", "body": "b", "needs": [], "surfaces": surfaces}]
        answers = {
            "decompose": {**DECOMPOSITION, "tasks": tasks},
            "review_adversary": [ACCEPTED, {"corrections": [correction], "summary": "ok"}],
        }
        runner = ScriptedRunner(answers)
        initiative_decompose.run({"run_id": "r", "date": "d", "cartridge": cart, "idea": "x", "tree": tree}, runner)
        adversary = [c for c in runner.calls if c["role"] == "review_adversary"]
        assert len(adversary) == 2, name
        assert [c["tier"] for c in adversary] == ["deep", "deep"], name


# ── router decisions: each call gets one from the DecisionSource ─────────────

DECISION = RouterDecision(
    chosen_class="deep", model="m", effort="high", budget_usd=1.0, reasons=("adversary",), clipped_by=()
)


class DecidedRunner(ScriptedRunner):
    """A `ScriptedRunner` that accepts `router_decision` and records it, None when the caller passed none."""

    def run(self, *, router_decision=None, **kwargs):
        try:
            return super().run(**kwargs)
        finally:
            self.calls[-1] = {**self.calls[-1], "router_decision": router_decision}


def _adversary_runs(cart, **extra):
    """Both adversary scenarios: edge challenge plus an unbuildable correction, and plus a lint correction."""
    cart["skills"]["review_adversary"] = "acme-skills:review-adversary"
    surface = {"task": "t1", "surface": "widget-thing", "replacement": "schema.py"}
    reach = {"task": "t1", "surface": "~/scratch/notes.md", "replacement": "graphs/notes.py"}
    scenarios = [
        (["widget-thing"], [{"repo": "graphs", "path": "graphs/schema.py"}], surface),
        (["~/scratch/notes.md"], [], reach),
    ]
    runners = []
    for surfaces, tree, correction in scenarios:
        tasks = [{"id": "t1", "phase": "p1", "title": "a", "body": "b", "needs": [], "surfaces": surfaces}]
        answers = {
            "decompose": {**DECOMPOSITION, "tasks": tasks},
            "review_adversary": [ACCEPTED, {"corrections": [correction], "summary": "ok"}],
        }
        runner = DecidedRunner(answers)
        initiative_decompose.run(
            {"run_id": "r", "date": "d", "cartridge": cart, "idea": "x", "tree": tree, **extra}, runner
        )
        runners.append(runner)
    return runners


def test_every_call_receives_the_decision_and_the_adversary_keeps_tier_deep(cart) -> None:
    for runner in _adversary_runs(cart, decision_source=lambda role, hints: DECISION):
        assert [c["role"] for c in runner.calls] == ["decompose", "review_adversary", "review_adversary"]
        assert all(c["router_decision"] is DECISION for c in runner.calls)
        assert [c["tier"] for c in runner.calls] == [None, "deep", "deep"]


def test_the_source_is_asked_with_the_decompose_hints_and_none_for_the_adversary(cart) -> None:
    asked = []

    def source(role, hints):
        asked.append((role, hints))

    _adversary_runs(cart, decision_source=source)
    per_run = [("decompose", Hints(judgment="high")), ("review_adversary", None), ("review_adversary", None)]
    assert asked == per_run * 2


def test_the_default_source_passes_no_decision(cart) -> None:
    for runner in _adversary_runs(cart):
        assert len(runner.calls) == 3
        assert all(c["router_decision"] is None for c in runner.calls)


def _settings_task(task_id, side, repo, needs=(), surfaces=()):
    return {
        "id": task_id,
        "phase": "p1",
        "title": task_id,
        "body": "b",
        "needs": list(needs),
        "surfaces": list(surfaces),
        "repo": repo,
        "interface": "settings",
        "side": side,
    }


def test_the_decompose_prompt_names_the_grants_it_is_given_and_no_other(cart) -> None:
    runner = ScriptedRunner({"decompose": DECOMPOSITION})
    args = {"run_id": "r", "date": "d", "cartridge": cart, "idea": "x", "grants": ["pytest", "mypy"]}
    initiative_decompose.run(args, runner)
    prompt = runner.calls[0]["prompt"]
    assert "pytest" in prompt
    assert "mypy" in prompt
    assert "git status" not in prompt
    assert "git diff" not in prompt


SETTINGS_TREE = [
    {"path": "agent_tools/settings.py", "repo": "tools"},
    {"path": "fixtures/settings.v1.json", "repo": "tools"},
    {"path": "src/settings.rs", "repo": "dash"},
]
PRODUCER = _settings_task("t1", "producer", "tools", surfaces=["agent_tools/settings.py"])
CONSUMER = _settings_task("t2", "consumer", "dash", surfaces=["src/settings.rs"])
CONTRACT = _settings_task("t0", "contract", "tools", surfaces=["fixtures/settings.v1.json"])


def _contract_run(cart, correction_tasks, **args):
    correction = {"corrections": [], "tasks": correction_tasks, "summary": "added the contract task"}
    cart["skills"]["review_adversary"] = "acme-skills:review-adversary"
    runner = ScriptedRunner({"decompose": {**DECOMPOSITION, "tasks": [PRODUCER, CONSUMER]}, "review_adversary": [ACCEPTED, correction]})
    base = {"run_id": "r", "date": "d", "cartridge": cart, "idea": "x", "tree": SETTINGS_TREE}
    return runner, initiative_decompose.run({**base, **args}, runner)


def test_a_cross_repo_interface_without_a_contract_task_is_corrected_with_one(cart) -> None:
    restated = [CONTRACT, dict(PRODUCER, needs=["t0"]), dict(CONSUMER, needs=["t0"], title="rewritten")]
    runner, result = _contract_run(cart, restated)
    lint_call = runner.calls[-1]
    assert lint_call["role"] == "review_adversary"
    assert "t2: contract" in lint_call["prompt"]
    by_id = {t["id"]: t for t in result["tasks"]}
    assert {i: t["needs"] for i, t in by_id.items()} == {"t0": [], "t1": ["t0"], "t2": ["t0"]}
    assert by_id["t0"]["side"] == "contract"
    assert by_id["t0"]["surfaces"] == ["fixtures/settings.v1.json"]
    assert by_id["t2"]["title"] == "t2"


def test_tasks_a_correction_adds_are_prefixed_with_the_initiative_id(cart) -> None:
    restated = [CONTRACT, dict(PRODUCER, needs=["t0"]), dict(CONSUMER, needs=["t0"])]
    _, result = _contract_run(cart, restated, initiative_id="i")
    needs = {t["id"]: t["needs"] for t in result["tasks"]}
    assert needs == {"i-t0": [], "i-t1": ["i-t0"], "i-t2": ["i-t0"]}


def test_a_correction_that_draws_a_cycle_is_refused(cart) -> None:
    restated = [dict(CONTRACT, needs=["t2"]), dict(PRODUCER, needs=["t0"]), dict(CONSUMER, needs=["t0"])]
    with pytest.raises(ContractViolation, match="corrected graph contains a dependency cycle"):
        _contract_run(cart, restated)


def test_a_correction_that_adds_a_task_on_a_surface_outside_the_tree_is_refused(cart) -> None:
    stray = dict(CONTRACT, surfaces=["elsewhere/settings.v1.json"])
    restated = [stray, dict(PRODUCER, needs=["t0"]), dict(CONSUMER, needs=["t0"])]
    with pytest.raises(ContractViolation, match="t0: elsewhere/settings.v1.json"):
        _contract_run(cart, restated)


def test_apply_corrections_opens_the_plan_with_an_added_task_and_takes_only_needs_from_a_restated_one() -> None:
    fixed = initiative_decompose._apply_corrections([PRODUCER], [], [CONTRACT, dict(PRODUCER, needs=["t0"], body="z")])
    assert [t["id"] for t in fixed] == ["t0", "t1"]
    assert fixed[1] == dict(PRODUCER, needs=["t0"])


def test_a_plan_inside_one_repository_gets_no_contract_demand(cart) -> None:
    tasks = [
        _settings_task("t1", "producer", "tools", surfaces=["agent_tools/settings.py"]),
        _settings_task("t2", "consumer", "tools", surfaces=["agent_tools/reader.py"]),
    ]
    cart["skills"]["review_adversary"] = "acme-skills:review-adversary"
    runner = ScriptedRunner({"decompose": {**DECOMPOSITION, "tasks": tasks}, "review_adversary": ACCEPTED})
    result = initiative_decompose.run({"run_id": "r", "date": "d", "cartridge": cart, "idea": "x"}, runner)
    assert [c["role"] for c in runner.calls] == ["decompose", "review_adversary"]
    assert not any("Ticket lint refused" in c["prompt"] for c in runner.calls)
    assert {t["id"]: (t["side"], t["needs"]) for t in result["tasks"]} == {"t1": ("producer", []), "t2": ("consumer", [])}


INTAKE = {
    "id": "Fix-Parser",
    "title": "make parse_row return None on a short row",
    "body": "parse_row raises on a short row.",
    "checks": ["pytest -q tests/test_parser.py"],
}
TREE = [{"repo": "r", "path": "src/parser.py"}]
TASK_VERDICT = {"verdict": "task", "reason": "One function in one file.", "surfaces": ["src/parser.py"]}
EPIC_VERDICT = {"verdict": "epic", "reason": "It spans two phases."}


def _sized(cart, responses, intake=INTAKE, tree=TREE):
    sized = {**cart, "skills": {**cart["skills"], "size": "acme-skills:size"}}
    runner = ScriptedRunner(responses)
    args = {"run_id": "r", "date": "d", "cartridge": sized, "idea": "x", "intake": intake, "tree": tree}
    return initiative_decompose.run(args, runner), runner


def _roles(runner) -> list[str]:
    return [c["role"] for c in runner.calls]


def test_a_one_file_intake_becomes_one_ticket_and_never_calls_decompose_or_scope_epic(cart) -> None:
    result, runner = _sized(cart, {"size": TASK_VERDICT})
    assert _roles(runner) == ["size"]
    assert runner.calls[0]["tier"] == "cheap"
    assert [t["id"] for t in result["tasks"]] == ["fix-parser"]
    ticket = next(p for p in result["proposals"] if p["target"] == "fix-parser")
    assert ticket["rationale"] == INTAKE["body"]
    frontmatter = yaml.safe_load(ticket["suggested_action"].split("frontmatter\n", 1)[1].split("body = ", 1)[0])
    assert frontmatter["title"] == INTAKE["title"]
    assert frontmatter["surfaces"] == ["src/parser.py"]
    assert frontmatter["checks"] == INTAKE["checks"]
    assert [p["target"] for p in result["proposals"]] == ["fix-parser", "initiative"]


def test_a_surface_not_in_the_repository_is_marked_new_on_the_task_path(cart) -> None:
    sized = {**cart, "skills": {**cart["skills"], "size": "acme-skills:size"}}
    result = initiative_decompose.run(
        {"run_id": "r", "date": "d", "cartridge": sized, "idea": "x", "intake": INTAKE},
        ScriptedRunner({"size": dict(TASK_VERDICT, surfaces=["src/fresh.py"])}),
    )
    assert result["tasks"][0]["surfaces"] == ["src/fresh.py (new)"]


def test_a_multi_phase_intake_runs_the_epic_path_and_matches_the_unsized_output(cart) -> None:
    before = initiative_decompose.run(
        {"run_id": "r", "date": "d", "cartridge": cart, "idea": "x"}, ScriptedRunner({"decompose": DECOMPOSITION})
    )
    after, runner = _sized(cart, {"size": EPIC_VERDICT, "decompose": DECOMPOSITION}, tree=[])
    assert _roles(runner) == ["size", "decompose"]
    assert _without_size_lines(after) == before


def _without_size_lines(result: dict) -> dict:
    """The result with the initiative's `size*` frontmatter lines removed, which only a sized run carries."""
    return {
        **result,
        "proposals": [
            dict(p, suggested_action="\n".join(ln for ln in p["suggested_action"].split("\n") if not ln.startswith("size")))
            if p["target"] == "initiative"
            else p
            for p in result["proposals"]
        ],
    }


def test_frontmatter_size_epic_overrides_a_task_verdict(cart) -> None:
    _, runner = _sized(cart, {"size": TASK_VERDICT, "decompose": DECOMPOSITION}, dict(INTAKE, frontmatter={"size": "epic"}), tree=[])
    assert _roles(runner) == ["size", "decompose"]


def test_frontmatter_size_task_overrides_an_epic_verdict_that_named_surfaces(cart) -> None:
    verdict = dict(EPIC_VERDICT, surfaces=["src/parser.py"])
    assert initiative_decompose._size_route(verdict, {"size": "task"}).source == "override"
    result, runner = _sized(cart, {"size": TASK_VERDICT}, dict(INTAKE, frontmatter={"size": "task"}))
    assert _roles(runner) == ["size"]
    assert len(result["tasks"]) == 1


def test_frontmatter_size_task_on_an_epic_verdict_with_no_surfaces_cannot_build_a_ticket_and_runs_epic(cart) -> None:
    _, runner = _sized(cart, {"size": EPIC_VERDICT, "decompose": DECOMPOSITION}, dict(INTAKE, frontmatter={"size": "task"}), tree=[])
    assert _roles(runner) == ["size", "decompose"]


def test_a_malformed_verdict_or_a_bad_override_falls_back_to_the_epic_path(cart) -> None:
    for verdict, frontmatter in (({"verdict": "huge", "reason": "No."}, {}), (TASK_VERDICT, {"size": "huge"})):
        _, runner = _sized(cart, {"size": verdict, "decompose": DECOMPOSITION}, dict(INTAKE, frontmatter=frontmatter), tree=[])
        assert _roles(runner) == ["size", "decompose"]


def test_without_a_bound_size_role_no_size_call_is_made(cart) -> None:
    runner = ScriptedRunner({"decompose": DECOMPOSITION})
    initiative_decompose.run({"run_id": "r", "date": "d", "cartridge": cart, "idea": "x", "intake": INTAKE}, runner)
    assert _roles(runner) == ["decompose"]


def test_size_prompt_carries_the_intake_the_files_and_the_threshold() -> None:
    prompt = initiative_decompose._size_prompt("fix the parser", ["src/parser.py"], "tickets_min: 3")
    assert "fix the parser" in prompt and "src/parser.py" in prompt and "tickets_min: 3" in prompt


def test_the_task_ticket_goes_through_ticket_lint(cart, monkeypatch) -> None:
    seen: list[list[str]] = []
    real = initiative_decompose.lint_tickets

    def spy(tasks, *rest):
        seen.append([t["id"] for t in tasks])
        return real(tasks, *rest)

    monkeypatch.setattr(initiative_decompose, "lint_tickets", spy)
    _sized(cart, {"size": TASK_VERDICT})
    assert seen == [["fix-parser"]]


def _recorded(cart, responses, tmp_path, intake=INTAKE, tree=TREE):
    """Run a sized graph with a runs_dir; return the initiative's parsed frontmatter and the ledger lines."""
    sized = {**cart, "skills": {**cart["skills"], "size": "acme-skills:size"}}
    runner = ScriptedRunner(responses)
    runner.runs_dir = tmp_path
    args = {"run_id": "r", "date": "d", "cartridge": sized, "idea": "x", "intake": intake, "tree": tree}
    result = initiative_decompose.run(args, runner)
    action = next(p for p in result["proposals"] if p["target"] == "initiative")["suggested_action"]
    frontmatter = yaml.safe_load(action.split("---\n")[1])
    ledger = tmp_path / "r.size.jsonl"
    lines = [json.loads(line) for line in ledger.read_text().splitlines()] if ledger.exists() else []
    return frontmatter, lines


def test_the_task_path_records_size_on_the_initiative_and_in_the_ledger(cart, tmp_path) -> None:
    frontmatter, lines = _recorded(cart, {"size": TASK_VERDICT}, tmp_path)
    assert (frontmatter["size"], frontmatter["size_reason"], frontmatter["size_source"]) == (
        "task",
        "One function in one file.",
        "model",
    )
    assert lines == [
        {
            "run_id": "r",
            "date": "d",
            "graph": "initiative-decompose",
            "event": "size",
            "size": "task",
            "size_reason": "One function in one file.",
            "size_source": "model",
            "skipped": ["scope_epic", "phase_planning"],
        }
    ]


def test_the_epic_path_records_size_epic_and_no_skipped_marker(cart, tmp_path) -> None:
    frontmatter, lines = _recorded(cart, {"size": EPIC_VERDICT, "decompose": DECOMPOSITION}, tmp_path, tree=[])
    assert (frontmatter["size"], frontmatter["size_reason"], frontmatter["size_source"]) == (
        "epic",
        "It spans two phases.",
        "model",
    )
    assert len(lines) == 1 and "skipped" not in lines[0]
    assert (lines[0]["size"], lines[0]["size_reason"], lines[0]["size_source"]) == ("epic", "It spans two phases.", "model")


def test_a_frontmatter_override_records_size_source_override(cart, tmp_path) -> None:
    frontmatter, lines = _recorded(
        cart, {"size": TASK_VERDICT, "decompose": DECOMPOSITION}, tmp_path, dict(INTAKE, frontmatter={"size": "epic"}), tree=[]
    )
    assert (frontmatter["size"], frontmatter["size_source"], frontmatter["size_reason"]) == (
        "epic",
        "override",
        "intake frontmatter sets size: epic",
    )
    assert [(ln["size"], ln["size_source"]) for ln in lines] == [("epic", "override")]


def test_a_malformed_verdict_records_the_fallback_with_its_reason(cart, tmp_path) -> None:
    frontmatter, lines = _recorded(
        cart, {"size": {"verdict": "huge", "reason": "No."}, "decompose": DECOMPOSITION}, tmp_path, tree=[]
    )
    expected = ("epic", "verdict must be task or epic, got 'huge'", "fallback")
    assert (frontmatter["size"], frontmatter["size_reason"], frontmatter["size_source"]) == expected
    assert [(ln["size"], ln["size_reason"], ln["size_source"]) for ln in lines] == [expected]
    assert "skipped" not in lines[0]


def test_a_task_verdict_that_cannot_become_a_ticket_records_the_fallback(cart, tmp_path) -> None:
    intake = {k: v for k, v in INTAKE.items() if k != "body"}
    frontmatter, lines = _recorded(cart, {"size": TASK_VERDICT, "decompose": DECOMPOSITION}, tmp_path, intake, tree=[])
    assert (frontmatter["size"], frontmatter["size_source"], frontmatter["size_reason"]) == (
        "epic",
        "fallback",
        "intake is missing body",
    )
    assert "skipped" not in lines[0]


def test_a_reason_with_a_colon_round_trips_through_the_frontmatter(cart, tmp_path) -> None:
    reason = "One file: parser.py only."
    frontmatter, lines = _recorded(cart, {"size": dict(TASK_VERDICT, reason=reason)}, tmp_path)
    assert frontmatter["size_reason"] == reason
    assert lines[0]["size_reason"] == reason


def test_no_runs_dir_writes_no_ledger_and_no_size_step_writes_no_size_lines(cart, tmp_path) -> None:
    _sized(cart, {"size": TASK_VERDICT})
    assert list(tmp_path.iterdir()) == []
    result = initiative_decompose.run(
        {"run_id": "r", "date": "d", "cartridge": cart, "idea": "x"}, ScriptedRunner({"decompose": DECOMPOSITION})
    )
    initiative = next(p for p in result["proposals"] if p["target"] == "initiative")
    assert "size" not in yaml.safe_load(initiative["suggested_action"].split("---\n")[1])


def _deploy_script(tmp_path) -> str:
    return str(tmp_path / "deploy" / "release.sh")


def _decompose_naming(path: str) -> dict:
    task = {"id": "t1", "phase": "p1", "title": "a", "body": f"run {path} once", "needs": [], "surfaces": []}
    return {**DECOMPOSITION, "tasks": [task]}


def test_an_idea_naming_a_path_outside_the_repo_is_refused_before_any_model_call(cart, tmp_path) -> None:
    runner = ScriptedRunner({"decompose": DECOMPOSITION})
    idea = f"ship it with {_deploy_script(tmp_path)}"
    with pytest.raises(initiative_decompose.LintRefusal) as caught:
        initiative_decompose.run({"run_id": "r", "date": "d", "cartridge": cart, "idea": idea}, runner)
    assert str(caught.value).startswith("lint-refused: idea reach")
    assert runner.calls == []


def test_an_intake_reach_allow_prefix_lets_the_idea_reach_decompose_and_admits_a_ticket_under_it(cart, tmp_path) -> None:
    script = _deploy_script(tmp_path)
    intake = {"id": "i", "title": "t", "body": "b", "checks": [], "frontmatter": {"reach_allow": [str(tmp_path / "deploy")]}}
    runner = ScriptedRunner({"decompose": _decompose_naming(script)})
    result = initiative_decompose.run(
        {"run_id": "r", "date": "d", "cartridge": cart, "idea": f"ship it with {script}", "intake": intake}, runner
    )
    assert [c["role"] for c in runner.calls] == ["decompose"]
    assert any(p["target"] == "t1" for p in result["proposals"])


def test_an_idea_whose_only_rooted_path_is_its_frontmatter_repo_line_is_not_refused(cart, tmp_path) -> None:
    idea = f"---\nrepo: {tmp_path / 'checkout'}\n---\nadd a retry to the parser"
    runner = ScriptedRunner({"decompose": DECOMPOSITION})
    initiative_decompose.run({"run_id": "r", "date": "d", "cartridge": cart, "idea": idea}, runner)
    assert [c["role"] for c in runner.calls] == ["decompose"]


def test_the_ideas_own_frontmatter_reach_allow_applies_when_no_intake_is_given(cart, tmp_path) -> None:
    script = _deploy_script(tmp_path)
    idea = f"---\nreach_allow:\n  - {tmp_path / 'deploy'}\n---\nship it with {script}"
    runner = ScriptedRunner({"decompose": _decompose_naming(script)})
    initiative_decompose.run({"run_id": "r", "date": "d", "cartridge": cart, "idea": idea}, runner)
    assert [c["role"] for c in runner.calls] == ["decompose"]


@pytest.mark.parametrize(
    ("frontmatter", "expected"),
    [
        ({"reach_allow": ["a", "b"]}, ["a", "b"]),
        ({"reach_allow": "a"}, []),
        ({"reach_allow": ["a", 3]}, []),
        ({}, []),
    ],
)
def test_reach_allow_is_a_list_of_strings_or_empty(frontmatter, expected) -> None:
    assert initiative_decompose._reach_allow({"intake": {"frontmatter": frontmatter}}, {}) == expected


CONTRACT_REFUSAL = (
    "contract task has no .json surface (give the contract task a `.json` fixture surface in the producer repository)"
)
PROSE_CONTRACT_PLAN = {
    **DECOMPOSITION,
    "tasks": [
        _settings_task("t0", "contract", "tools", surfaces=["agent_tools/settings.py"]),
        dict(PRODUCER, needs=["t0"]),
        dict(CONSUMER, needs=["t0"]),
    ],
}
JSON_SURFACE_FIX = {
    "corrections": [{"task": "t0", "surface": "agent_tools/settings.py", "replacement": "fixtures/settings.v1.json"}],
    "tasks": [],
    "summary": "gave the contract task its fixture",
}
NO_CHANGE = {"corrections": [], "tasks": [], "summary": "no change"}


def _unbound_contract_args(cart):
    return {"run_id": "r", "date": "d", "cartridge": cart, "idea": "x", "tree": SETTINGS_TREE}


class SchemaSpy(ScriptedRunner):
    """ScriptedRunner that also keeps the schema each call was made with."""

    def __init__(self, responses) -> None:
        super().__init__(responses)
        self.schemas: list[dict] = []

    def run(self, **kwargs):
        self.schemas.append(kwargs["schema"])
        return super().run(**kwargs)


def test_a_contract_refusal_is_put_to_the_seat_once_and_the_corrected_plan_passes(cart) -> None:
    runner = SchemaSpy({"decompose": [PROSE_CONTRACT_PLAN, JSON_SURFACE_FIX]})
    result = initiative_decompose.run(_unbound_contract_args(cart), runner)
    assert [c["role"] for c in runner.calls] == ["decompose", "decompose"]
    assert runner.schemas[1] is initiative_decompose.UNBUILDABLE_SCHEMA
    second = runner.calls[1]["prompt"]
    assert CONTRACT_REFUSAL in second
    assert "Break this idea into phases" not in second
    # The seat is shown the surface its correction must replace, verbatim.
    assert "t0: agent_tools/settings.py" in second
    assert JSON_SURFACE_FIX["corrections"][0]["surface"] in second
    by_id = {t["id"]: t for t in result["tasks"]}
    assert by_id["t0"]["surfaces"] == ["fixtures/settings.v1.json"]


def test_a_bound_adversary_still_gets_the_contract_refusal_with_the_surfaces(cart) -> None:
    cart["skills"]["review_adversary"] = "acme-skills:review-adversary"
    runner = ScriptedRunner({"decompose": PROSE_CONTRACT_PLAN, "review_adversary": [ACCEPTED, JSON_SURFACE_FIX]})
    result = initiative_decompose.run(_unbound_contract_args(cart), runner)
    assert [c["role"] for c in runner.calls] == ["decompose", "review_adversary", "review_adversary"]
    assert CONTRACT_REFUSAL in runner.calls[-1]["prompt"]
    assert "t0: agent_tools/settings.py" in runner.calls[-1]["prompt"]
    assert {t["id"]: t for t in result["tasks"]}["t0"]["surfaces"] == ["fixtures/settings.v1.json"]


def test_a_reach_refusal_with_no_adversary_still_quarantines_without_a_second_ask(cart) -> None:
    plan = {
        **DECOMPOSITION,
        "tasks": [{"id": "t1", "phase": "p1", "title": "a", "body": "b", "needs": [], "surfaces": ["~/scratch/notes.md"]}],
    }
    runner = ScriptedRunner({"decompose": plan})
    with pytest.raises(initiative_decompose.LintRefusal, match="reach"):
        initiative_decompose.run({"run_id": "r", "date": "d", "cartridge": cart, "idea": "x"}, runner)
    assert [c["role"] for c in runner.calls] == ["decompose"]


def test_a_second_contract_refusal_fails_the_run_with_the_lint_text_and_no_third_ask(cart) -> None:
    runner = ScriptedRunner({"decompose": [PROSE_CONTRACT_PLAN, NO_CHANGE]})
    with pytest.raises(initiative_decompose.LintRefusal) as refused:
        initiative_decompose.run(_unbound_contract_args(cart), runner)
    assert CONTRACT_REFUSAL in str(refused.value)
    assert [c["role"] for c in runner.calls] == ["decompose", "decompose"]


INTAKE_FILE = '---\ntitle: "Name the initiative: from intake"\nrepo: ~/repos/acme\n---\n\nThe why, in prose.\n'


def test_an_intake_file_yields_its_frontmatter_title_and_repo_and_a_body_without_frontmatter() -> None:
    title, repo, body = initiative_decompose.idea_parts(INTAKE_FILE)
    assert (title, repo) == ("Name the initiative: from intake", "~/repos/acme")
    assert body == "The why, in prose.\n"


@pytest.mark.parametrize(
    ("idea", "title", "body"),
    [
        ("---\nrepo: r\nno close", "---", "---\nrepo: r\nno close"),
        ("---\n- a\n---\nbody", "body", "body"),
    ],
)
def test_an_unclosed_or_non_mapping_frontmatter_block_gives_no_repo(idea, title, body) -> None:
    assert initiative_decompose.idea_parts(idea) == (title, "", body)


def test_an_idea_with_only_a_heading_takes_the_heading_as_title() -> None:
    title, repo, body = initiative_decompose.idea_parts("\nintro line\n# The Heading\nmore\n")
    assert (title, repo) == ("The Heading", "")
    assert body == "\nintro line\n# The Heading\nmore\n"


def test_an_idea_with_neither_takes_the_first_non_empty_line() -> None:
    assert initiative_decompose.idea_parts("\n\n  go arrow-native  \nsecond\n")[0] == "go arrow-native"


def test_a_title_longer_than_120_characters_is_cut_to_120() -> None:
    assert len(initiative_decompose.idea_parts("x" * 200)[0]) == 120


def test_a_multi_line_title_is_refused_naming_the_initiative() -> None:
    with pytest.raises(ContractViolation, match="init-x"):
        initiative_decompose.initiative_text({"id": "init-x", "title": "a\nb", "why": "w"}, ["p1"], {}, "")
