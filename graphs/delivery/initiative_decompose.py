"""initiative-decompose — a large idea into phases and a task DAG.

    decompose -> adversary -> emit

The front of the pipeline, and the step that makes everything after it
parallelisable. An idea arrives as prose; what comes out is phases, tasks, and
the dependency edges between them — and one `item_create` proposal per task.

The adversarial pass here is the highest-leverage one in the system, and it has
a single job: **attack the dependency edges.** Every edge that is not real
serialises work that could have run at once, and the person who just drew the
graph is the last person likely to notice they drew too many. An edge that
exists because the work "feels sequential" costs a phase its parallelism, and
nothing else in the pipeline will ever question it.

Strictly propose-only, like everything else — the tasks land as proposals, and
the work store is written by an apply arm after a human said yes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from graphs._contract import ContractViolation, epic_shape, landing_for, proposal, require, require_cartridge
from runner.protocol import NodeRunner

__all__ = ["GRAPH_NAME", "run", "initiative_text"]

GRAPH_NAME = "initiative-decompose"

DECOMPOSE_SCHEMA = {
    "type": "object",
    "properties": {
        "phases": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "goal": {"type": "string"}},
                "required": ["id", "goal"],
                "additionalProperties": False,
            },
        },
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "phase": {"type": "string"},
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                    "needs": {"type": "array", "items": {"type": "string"}},
                    "surfaces": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id", "phase", "title", "body", "needs", "surfaces"],
                "additionalProperties": False,
            },
        },
        "rationale": {"type": "string"},
    },
    "required": ["phases", "tasks", "rationale"],
    "additionalProperties": False,
}

EDGE_CHALLENGE_SCHEMA = {
    "type": "object",
    "properties": {
        "spurious_edges": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "task": {"type": "string"},
                    "needs": {"type": "string"},
                    "why_not_real": {"type": "string"},
                },
                "required": ["task", "needs", "why_not_real"],
                "additionalProperties": False,
            },
        },
        "missing_edges": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "task": {"type": "string"},
                    "needs": {"type": "string"},
                    "why_real": {"type": "string"},
                },
                "required": ["task", "needs", "why_real"],
                "additionalProperties": False,
            },
        },
        "verdict": {"type": "string", "enum": ["accept", "revise"]},
        "summary": {"type": "string"},
    },
    "required": ["spurious_edges", "missing_edges", "verdict", "summary"],
    "additionalProperties": False,
}


def initiative_text(idea: Mapping[str, Any], phases: Sequence[str], goals: Mapping[str, str], repo: str) -> str:
    """The `initiative.md` shape every hand-written initiative in the workspace carries."""
    goal_lines = "\n".join(f"- {phase_id}: {goals.get(phase_id, '')}" for phase_id in phases)
    return (
        "---\n"
        f"id: {idea.get('id')}\n"
        f"title: {idea.get('title')}\n"
        f"repo: {repo}\n"
        f"budget_usd: {idea.get('budget_usd')}\n"
        "---\n\n"
        f"{idea.get('why', '')}\n\n"
        "PHASE GOALS, each judged against ITS OWN line:\n"
        f"{goal_lines}\n"
    )


def _apply_challenge(tasks: list[dict[str, Any]], challenge: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Drop edges the adversary showed were not real; add ones it showed were.

    Both directions matter. Dropping a spurious edge buys parallelism; adding a
    missing one prevents a task starting on ground that is not there yet, which
    is the failure the parallelism would otherwise cause.
    """
    by_id = {t["id"]: t for t in tasks}

    for edge in challenge.get("spurious_edges") or []:
        task = by_id.get(str(edge.get("task")))
        if task and str(edge.get("needs")) in task["needs"]:
            task["needs"] = [n for n in task["needs"] if n != str(edge.get("needs"))]

    for edge in challenge.get("missing_edges") or []:
        task, need = by_id.get(str(edge.get("task"))), str(edge.get("needs"))
        # Only between tasks that exist, and never a self-edge — the adversary
        # does not get to invent a task or stall one on itself.
        if task and need in by_id and need != task["id"] and need not in task["needs"]:
            task["needs"].append(need)

    return list(by_id.values())


def _local_cycle(tasks: list[dict[str, Any]]) -> list[str]:
    """Cheap cycle check before anything is proposed. The store checks again."""
    by_id = {t["id"]: t for t in tasks}
    problems: list[str] = []
    colour: dict[str, int] = dict.fromkeys(by_id, 0)

    def visit(node: str, trail: list[str]) -> None:
        colour[node] = 1
        for need in by_id[node].get("needs") or []:
            if need not in by_id:
                continue
            if colour[need] == 1:
                problems.append(" -> ".join([*trail, need]))
            elif colour[need] == 0:
                visit(need, [*trail, need])
        colour[node] = 2

    for node in sorted(by_id):
        if colour[node] == 0:
            visit(node, [node])
    return problems


def run(args: Mapping[str, Any], runner: NodeRunner) -> dict[str, Any]:
    """Run the graph. The idea arrives as an argument; nothing is read from disk."""
    cartridge = require_cartridge(args)
    run_id, date, idea = require(args, "run_id", "date", "idea")

    bound = cartridge.get("skills") or {}
    if "decompose" not in bound:
        raise ContractViolation(
            "this graph needs the optional role 'decompose' bound in the cartridge; "
            "a team that has not bound it cannot decompose an initiative"
        )

    context = list(cartridge.get("context") or [])

    decomposition = dict(
        runner.run(
            role="decompose",
            tier="standard",
            schema=DECOMPOSE_SCHEMA,
            context=context,
            prompt=(
                f"Break this idea into phases and tasks.\n\nIdea: {idea}\nDate: {date}\n\n"
                "Phases are ordered; tasks within a phase are not necessarily. Draw a "
                "dependency edge ONLY where order genuinely matters — an edge that exists "
                "because the work feels sequential blocks work that could have run in "
                "parallel. Name the surfaces each task touches."
            ),
        )
    )

    initiative_id = args.get("initiative_id")

    tasks = [dict(t, needs=list(t.get("needs") or []), surfaces=list(t.get("surfaces") or [])) for t in decomposition.get("tasks") or []]
    if not tasks:
        raise ContractViolation("decompose returned no tasks; there is nothing to propose")

    if initiative_id:
        tasks = [
            dict(t, id=f"{initiative_id}-{t['id']}", needs=[f"{initiative_id}-{n}" for n in t["needs"]])
            for t in tasks
        ]

    challenge: dict[str, Any] | None = None
    if "review_adversary" in bound:
        challenge = dict(
            runner.run(
                role="review_adversary",
                tier="deep",
                schema=EDGE_CHALLENGE_SCHEMA,
                context=context,
                prompt=(
                    "Attack this dependency graph. Your job is to find edges that are "
                    "not real — every one of them serialises work that could have run at "
                    "the same time.\n\n"
                    f"Idea: {idea}\nPhases: {decomposition.get('phases')}\nTasks: {tasks}\n\n"
                    "For each edge you challenge, say why the dependency does not "
                    "actually hold. Also name any edge that IS real and is missing."
                ),
            )
        )
        tasks = _apply_challenge(tasks, challenge)

    cycles = _local_cycle(tasks)
    if cycles:
        raise ContractViolation(
            "the decomposed graph contains a dependency cycle, so nothing in it could "
            "ever become ready: " + "; ".join(cycles)
        )

    shape = epic_shape(
        cartridge,
        phases=len({t["phase"] for t in tasks}),
        tickets=len(tasks),
        repos=len({s for t in tasks for s in t.get("surfaces") or []} & {"cross_repo"}) + 1,
    )
    landing = landing_for(cartridge, "planned")

    phase_order = [str(p.get("id")) for p in decomposition.get("phases") or []]
    goals = {str(p.get("id")): str(p.get("goal") or "") for p in decomposition.get("phases") or []}
    idea_doc = {"id": initiative_id or run_id, "title": str(idea), "budget_usd": args.get("budget_usd"), "why": str(idea)}
    initiative_body = initiative_text(idea_doc, phase_order, goals, str(args.get("repo") or ""))
    initiative_where = "/".join(part for part in (landing, initiative_id) if part)

    proposals = [
        proposal(
            cartridge,
            kind="item_create",
            target=str(task["id"]),
            evidence=[
                {"check": "phase", "output": str(task.get("phase"))},
                {"check": "depends on", "output": ", ".join(task["needs"]) or "nothing — can start immediately"},
                {"check": "surfaces", "output": ", ".join(task.get("surfaces") or []) or "none declared"},
                *(
                    [{"check": "adversary on the DAG", "output": "adversary edges applied: " + str(challenge.get("summary"))}]
                    if challenge
                    else []
                ),
            ],
            rationale=str(task.get("body") or decomposition.get("rationale", "")),
            # The whole item, in the action. The arm sees the proposal and
            # nothing else, so an action that named only an id would leave it
            # inventing the title and guessing the initiative — the first live
            # run proved exactly that. `initiative_id` is optional: absent, the
            # store root's name is the initiative, as `read_initiative` reads it.
            suggested_action=_item_action(task, landing=landing, initiative_id=initiative_id),
        )
        for task in sorted(tasks, key=lambda t: str(t["id"]))
    ] + [
        proposal(
            cartridge,
            kind="item_create",
            target="initiative",
            evidence=[{"check": "phases", "output": ", ".join(phase_order) or "none"}],
            rationale=str(decomposition.get("rationale") or ""),
            suggested_action=f"create {initiative_where}/initiative.md with body =\n{initiative_body}",
        )
    ]

    unblocked = [t["id"] for t in tasks if not t["needs"]]
    return {
        "run_id": run_id,
        "date": date,
        "idea": idea,
        "shape": shape,
        "phases": decomposition.get("phases") or [],
        "tasks": sorted(tasks, key=lambda t: str(t["id"])),
        "challenge": challenge,
        "proposals": proposals,
        "totals": {
            "phases": len({t["phase"] for t in tasks}),
            "tasks": len(tasks),
            "edges": sum(len(t["needs"]) for t in tasks),
            "edges_dropped": len((challenge or {}).get("spurious_edges") or []),
            "edges_added": len((challenge or {}).get("missing_edges") or []),
            "immediately_startable": len(unblocked),
        },
    }


def _item_action(task: Mapping[str, Any], *, landing: str, initiative_id: str | None) -> str:
    """Pure: the create action, carrying every field the work-item arm must write."""
    where = "/".join(part for part in (landing, initiative_id, str(task.get("phase"))) if part)
    # Empty stays `[]`, never a placeholder word: the arm copies this text into
    # frontmatter verbatim, and `needs: [none]` is an edge to a task that does
    # not exist — the third live run landed exactly that and the DAG refused.
    needs = ", ".join(str(n) for n in task.get("needs") or [])
    surfaces = ", ".join(str(x) for x in task.get("surfaces") or [])
    return (
        f"create {where}/{task['id']}.md with frontmatter id={task['id']}, "
        f"title={task.get('title') or task['id']!s}, phase={task.get('phase')}, state=ready, "
        f"needs=[{needs}], surfaces=[{surfaces}]; body = the rationale"
    )


from graphs._spec import GraphSpec, Need  # noqa: E402

SPEC = GraphSpec(
    name="decompose",
    graph_name=GRAPH_NAME,
    run=run,
    summary="an idea into phases and a task DAG, with the edges attacked before anyone trusts them",
    needs=(
        Need("idea", flag="--idea", kind="text_or_path",
             help="the initiative, as prose or a path to a file holding it"),
        Need("initiative_id", flag="--initiative-id", required=False,
             help="directory name for the initiative under the work store (default: the store root itself)"),
        Need("repo", flag="--target-repo", required=False,
             help="the repository the initiative targets, for initiative.md's frontmatter"),
        Need("budget_usd", flag="--budget-usd", required=False,
             help="the initiative's budget in dollars, for initiative.md's frontmatter"),
    ),
)
