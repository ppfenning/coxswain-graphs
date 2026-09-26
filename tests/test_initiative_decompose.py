"""Decomposition, and the adversary whose job is to delete dependency edges.

Every edge that is not real serialises work that could have run at once, and
the person who just drew the graph is the last person likely to spot one.
"""

from __future__ import annotations

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


def test_prefixed_adds_the_initiative_id_to_a_bare_id() -> None:
    assert initiative_decompose._prefixed("init", "a") == "init-a"


def test_prefixed_leaves_an_already_prefixed_id_alone() -> None:
    assert initiative_decompose._prefixed("init", "init-a") == "init-a"


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


def test_a_grant_problem_is_recorded_as_a_lint_entry_not_a_refusal(cart) -> None:
    decomposition = {
        **DECOMPOSITION,
        "tasks": [{"id": "t1", "phase": "p1", "title": "a", "body": "```bash\ncox route lint t1\n```", "needs": [], "surfaces": []}],
    }
    result = decompose(cart, decomposition)
    lint_entry = "grant: names `cox`, which is not granted (name only pytest, git status, git diff)"
    task = next(t for t in result["tasks"] if t["id"] == "t1")
    assert task["lint"] == [lint_entry]
    ticket = next(p for p in result["proposals"] if p["target"] == "t1")
    assert f"lint:\n- '{lint_entry}'" in ticket["suggested_action"]
    assert {"check": "lint", "output": lint_entry} in ticket["evidence"]


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


def test_a_grant_advisory_lands_under_lint_and_never_in_surfaces(cart) -> None:
    decomposition = {
        **DECOMPOSITION,
        "tasks": [
            {
                "id": "t1",
                "phase": "p1",
                "title": "a",
                "body": "```bash\ncox route lint t1\n```",
                "needs": [],
                "surfaces": ["graphs/schema.py"],
            },
        ],
    }
    result = decompose(cart, decomposition)
    lint_entry = "grant: names `cox`, which is not granted (name only pytest, git status, git diff)"
    task = next(t for t in result["tasks"] if t["id"] == "t1")
    assert task["lint"] == [lint_entry]
    assert task["surfaces"] == ["graphs/schema.py"]


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


def test_a_tools_ticket_naming_a_graphs_file_carries_a_cross_repo_lint_entry(cart) -> None:
    result = _decompose_for(cart, "coxswain-tools", ["harness/cli.py"])
    task = next(t for t in result["tasks"] if t["id"] == "t1")
    assert task["lint"] == [
        "cross_repo: names harness/cli.py, which lives in graphs "
        "(paste the code the build needs into the ticket: a build reads only its own repository)"
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
    decomposition = {
        **DECOMPOSITION,
        "tasks": [{"id": "t1", "phase": "p1", "title": "a", "body": "```bash\ncox route lint t1\n```", "needs": [], "surfaces": []}],
    }
    result = decompose(cart, decomposition)
    lint_entry = "grant: names `cox`, which is not granted (name only pytest, git status, git diff)"
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
