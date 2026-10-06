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

import re
from collections.abc import Mapping, Sequence
from typing import Any

import yaml

from graphs._contract import ContractViolation, epic_shape, landing_for, proposal, require, require_cartridge
from graphs.delivery.phase_split import split_same_phase_needs
from graphs.delivery.ticket_lint import Problem, lint_tickets
from runner.decision_log import RouterDecision
from runner.decision_source import DecisionSource, NoDecisionSource, ask
from runner.protocol import NodeRunner
from runner.tier_resolution import Hints

__all__ = [
    "GRAPH_NAME",
    "LintRefusal",
    "first_problem_line",
    "initiative_text",
    "resolve_surfaces",
    "run",
    "surface_problem",
    "unexecuted_item_create_reason",
]

# A `/` or a file suffix, no spaces, optional trailing `(new)` marker — the
# same shape lifecycle_propose._PATH_TOKEN checks. An all-caps bare token
# (LICENSE, README) has neither and is kept on the naming convention alone.
_PATH_TOKEN = re.compile(r"^[\w.\-]+(?:/[\w.\-]+)*$")


def _looks_like_a_surface(entry: str) -> bool:
    body = entry[: -len(" (new)")] if entry.endswith(" (new)") else entry
    if not _PATH_TOKEN.match(body):
        return False
    return "/" in body or bool(re.search(r"\.[A-Za-z0-9]{1,5}$", body)) or body.isupper()


def _edit_distance(a: str, b: str) -> int:
    """Levenshtein distance: insert, delete, and substitute each cost 1."""
    if not a:
        return len(b)
    if not b:
        return len(a)
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i] + [0] * len(b)
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            current[j] = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
        previous = current
    return previous[-1]


# The declared phase in a task is the seat's own transcription of a plan id it
# was just given — a one- or two-character slip, not a different word. Wider
# than that and a "correction" is a guess dressed as a fix, which is exactly
# what turned 'faulthandring-signal' into a silently accepted 'faulthandler-
# signal' would have been: that pair sits at distance 4, outside this budget,
# and the node fails instead of guessing.
_PHASE_EDIT_DISTANCE_BUDGET = 2


def _resolve_phase(phase: str, declared: Sequence[str]) -> tuple[str, str | None] | None:
    """Resolve `phase` against the plan's declared phases.

    `(phase, None)` on an exact match. `(matched, note)` when exactly one
    declared phase is within `_PHASE_EDIT_DISTANCE_BUDGET` — `note` names the
    correction. `None` when no declared phase is close enough, or more than
    one ties: an ambiguous near-miss is a defect for the caller to refuse,
    never a guess.
    """
    if phase in declared:
        return phase, None
    near = [d for d in declared if _edit_distance(phase, d) <= _PHASE_EDIT_DISTANCE_BUDGET]
    if len(near) == 1:
        return near[0], f'phase "{phase}" corrected to "{near[0]}"'
    return None


GRAPH_NAME = "initiative-decompose"


def _decision(source: DecisionSource, role: str, hints: Hints | None) -> dict[str, RouterDecision]:
    """`router_decision` kwarg for a runner call; empty when the source has no decision, so it defaults to None."""
    decision = ask(source, role, hints)
    return {} if decision is None else {"router_decision": decision}


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

UNBUILDABLE_SCHEMA = {
    "type": "object",
    "properties": {
        "corrections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "task": {"type": "string"},
                    "surface": {"type": "string"},
                    "replacement": {"type": "string"},
                },
                "required": ["task", "surface", "replacement"],
                "additionalProperties": False,
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["corrections", "summary"],
    "additionalProperties": False,
}


def _scalar(value: Any) -> str:
    """One YAML flow scalar, quoted only when YAML requires it."""
    dumped = yaml.safe_dump(value, default_flow_style=True, allow_unicode=True, width=10_000)
    return dumped.strip().removesuffix("...").rstrip()


def initiative_text(
    idea: Mapping[str, Any], phases: Sequence[str], goals: Mapping[str, str], repo: str, *, intake: str | None = None
) -> str:
    """The `initiative.md` shape every hand-written initiative in the workspace carries."""
    goal_lines = "\n".join(f"- {phase_id}: {goals.get(phase_id, '')}" for phase_id in phases)
    intake_line = f"intake: {_scalar(intake)}\n" if intake else ""
    return (
        "---\n"
        f"id: {_scalar(idea.get('id'))}\n"
        f"title: {_scalar(idea.get('title'))}\n"
        f"repo: {_scalar(repo)}\n"
        f"budget_usd: {_scalar(idea.get('budget_usd'))}\n"
        f"{intake_line}"
        "---\n\n"
        f"{idea.get('why', '')}\n\n"
        "PHASE GOALS, each judged against ITS OWN line:\n"
        f"{goal_lines}\n"
    )


def resolve_surfaces(surfaces: Sequence[str], tree: Sequence[Mapping[str, Any]]) -> tuple[list[str], list[str]]:
    """Match each prose surface against a real path in tree. (resolved paths, unresolved prose).

    Same name and signature as `core.workstore.resolve_surfaces` so a later
    `from core.workstore import resolve_surfaces, surface_problem` is a one-line
    swap — but a graph never imports `core` itself, so this stays the real
    implementation here, not a stand-in.
    """
    resolved: list[str] = []
    unresolved: list[str] = []
    for surface in surfaces:
        hit = next((str(row.get("path")) for row in tree if surface == row.get("path") or surface in str(row.get("path"))), None)
        (resolved if hit else unresolved).append(hit or surface)
    return resolved, unresolved


def surface_problem(unresolved: Sequence[str]) -> str | None:
    """One line per unresolved surface; `None` when there is nothing unbuildable."""
    if not unresolved:
        return None
    return "\n".join(f"unbuildable: surfaces are prose — {item}" for item in unresolved)


class LintRefusal(ContractViolation):
    """Ticket lint refused the decomposition for reach or coupling; the message is one problem per line."""


def first_problem_line(lines: Sequence[str]) -> str | None:
    """The first line with text, stripped of surrounding whitespace; None when every line is blank."""
    return next((s for s in (line.strip() for line in lines) if s), None)


def _lint_refusal_text(problems: Sequence[Problem]) -> str | None:
    """One line per reach/coupling `Problem`; `None` when there is nothing to refuse."""
    if not problems:
        return None
    return "\n".join(f"{p.task}: {p.rule} — {p.detail} ({p.fix})" for p in problems)


def _apply_surface_resolutions(tasks: list[dict[str, Any]], tree: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """New task dicts; each task's surfaces resolved against tree. The caller's tasks are untouched.

    Anything that does not resolve keeps its prose rather than being dropped —
    an unresolved surface is a defect for `run` to act on, never one this
    function silently discards.
    """
    new_tasks = []
    for task in tasks:
        resolved, unresolved = resolve_surfaces(task.get("surfaces") or [], tree)
        new_tasks.append(dict(task, surfaces=resolved + unresolved))
    return new_tasks


def _unresolved_pairs(tasks: Sequence[Mapping[str, Any]], tree: Sequence[Mapping[str, Any]]) -> list[str]:
    """`"task: prose"` for every surface still unresolved against tree, one per line-to-be."""
    return [
        f"{task['id']}: {surface}"
        for task in tasks
        for surface in resolve_surfaces(task.get("surfaces") or [], tree)[1]
    ]


def _tests_replacement(surface: str, tree: Sequence[Mapping[str, Any]]) -> str | None:
    """The `tests/`-rooted tree path whose file name matches `surface`'s, else None."""
    name = surface.rsplit("/", 1)[-1]
    return next(
        (
            str(row.get("path"))
            for row in tree
            if str(row.get("path")).startswith("tests/") and str(row.get("path")).rsplit("/", 1)[-1] == name
        ),
        None,
    )


def _validate_plan_surfaces(
    tasks: list[dict[str, Any]], tree: Sequence[Mapping[str, Any]]
) -> tuple[list[dict[str, Any]], list[str]]:
    """New task dicts with a real `tests/` path swapped in for a misplaced one; `(task, surface)` hard failures.

    Runs ahead of the prose-oriented resolution below: a surface that is
    `(new)`, is not path-shaped, or already resolves against `tree` is left
    for that code to handle exactly as it does today. What is left is a
    well-formed path that simply names the wrong location — a build defect,
    not prose for the adversary to interpret, so a `tests/` rename is
    corrected here and anything else fails the node outright.
    """
    new_tasks: list[dict[str, Any]] = []
    failures: list[str] = []
    for task in tasks:
        surfaces = task.get("surfaces") or []
        resolved, unresolved = resolve_surfaces(surfaces, tree)
        still_prose = set(unresolved)
        fixes: list[str] = []
        new_surfaces: list[str] = []
        for surface in surfaces:
            candidate = surface
            eligible = (
                candidate in still_prose
                and not candidate.endswith(" (new)")
                and _looks_like_a_surface(candidate)
            )
            if not eligible:
                new_surfaces.append(surface)
                continue
            replacement = _tests_replacement(candidate, tree)
            if replacement is None:
                failures.append(f"{task['id']}: {surface}")
                new_surfaces.append(surface)
                continue
            fixes.append(f"{surface} -> {replacement}")
            new_surfaces.append(replacement)
        new_tasks.append(dict(task, surfaces=new_surfaces, surface_fixes=fixes))
    return new_tasks, failures


def _drop_foreign_needs(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """New task dicts; a `needs` entry naming no id in this plan is removed and recorded.

    A foreign id names real work, just not work this plan orders by an edge —
    it lands on its own schedule, so dropping the edge is correct, not a
    workaround for a decompose mistake.
    """
    ids = {str(t["id"]) for t in tasks}
    new_tasks: list[dict[str, Any]] = []
    for task in tasks:
        needs = task.get("needs") or []
        kept = [n for n in needs if n in ids]
        dropped = [n for n in needs if n not in ids]
        new_tasks.append(dict(task, needs=kept, needs_dropped=dropped))
    return new_tasks


def _apply_corrections(tasks: list[dict[str, Any]], corrections: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """New task dicts; a corrected surface replaces the prose the adversary named, nothing else."""
    by_task: dict[str, dict[str, str]] = {}
    for c in corrections:
        by_task.setdefault(str(c.get("task")), {})[str(c.get("surface"))] = str(c.get("replacement"))
    return [
        dict(t, surfaces=[by_task.get(str(t["id"]), {}).get(s, s) for s in t.get("surfaces") or []])
        for t in tasks
    ]


def _split_cross_repo(tasks: list[dict[str, Any]], tree: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One task per repo when a task's resolved surfaces span more than one tree in the stack.

    A task that never touches more than one repo passes through unchanged. A
    task that was split is replaced everywhere it was named in `needs`, by
    every part it became — a downstream task waited on the whole thing, and
    now the whole thing is more than one task.
    """
    repo_of = {str(row.get("path")): str(row.get("repo")) for row in tree}
    parts: dict[str, list[str]] = {}
    split: list[dict[str, Any]] = []
    for task in tasks:
        repos = sorted({repo_of[s] for s in task.get("surfaces") or [] if s in repo_of})
        if len(repos) <= 1:
            parts[str(task["id"])] = [str(task["id"])]
            split.append(task)
            continue
        new_ids = [f"{task['id']}--{repo}" for repo in repos]
        parts[str(task["id"])] = new_ids
        split.extend(
            dict(task, id=new_id, surfaces=[s for s in task["surfaces"] if repo_of.get(s) == repo])
            for repo, new_id in zip(repos, new_ids, strict=True)
        )
    return [dict(t, needs=[n for need in t["needs"] for n in parts.get(need, [need])]) for t in split]


def _stack_repo_count(tasks: Sequence[Mapping[str, Any]], tree: Sequence[Mapping[str, Any]]) -> int:
    """The number of repos the stack actually touches, read off the tree rather than guessed."""
    if not tree:
        return 1
    repo_of = {str(row.get("path")): str(row.get("repo")) for row in tree}
    repos = {repo_of[s] for t in tasks for s in t.get("surfaces") or [] if s in repo_of}
    return len(repos) or 1


def _prefixed(initiative_id: str, task_id: str) -> str:
    """Scope a task id to its initiative once; an id the model already prefixed is kept."""
    return task_id if task_id.startswith(f"{initiative_id}-") else f"{initiative_id}-{task_id}"


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


def _ordinal_needs_problems(tasks: Sequence[Mapping[str, Any]]) -> list[str]:
    """One line per `needs` entry that names no id among the tasks the model listed.

    Ordinal mode asks the model for `t1`, `t2`, ... keys instead of full slugs;
    this is the check that a `needs` entry actually lands on one of them before
    anything downstream mints an id or writes a proposal from it.
    """
    ids = {str(t.get("id")) for t in tasks}
    return [
        f"{task.get('id')}: needs unknown key {need!r}"
        for task in tasks
        for need in task.get("needs") or []
        if need not in ids
    ]


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


def unexecuted_item_create_reason(diffs: Sequence[Mapping[str, Any]]) -> str | None:
    """The arm's own refusal, when every `item_create` diff was approved but none applied.

    `None` when there are no `item_create` diffs, or at least one landed —
    the run summary already counts that case correctly. `apply_decisions`
    (harness/gate.py) never passes the arm's free-text detail into
    `gate_diff` (core.manifest) — only `item`, `decision`, `applied`, and
    `edited` cross that boundary — so the refusal named here is the diff's
    own `outcome` field, the word `apply_decisions`'s docstring already
    promises for this case: `"skipped"`.
    """
    item_creates = [d for d in diffs if d.get("kind") == "item_create"]
    if not item_creates:
        return None
    if any(d.get("applied") for d in item_creates):
        return None
    if not all(d.get("decision") == "approved" for d in item_creates):
        return None
    return str(item_creates[0].get("outcome"))


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

    source = args.get("decision_source") or NoDecisionSource()
    context = list(cartridge.get("context") or [])

    task_ids = str(args.get("task_ids") or "slug")

    decompose_hints = Hints(judgment="high")
    decompose_prompt = (
        f"Break this idea into phases and tasks.\n\nIdea: {idea}\nDate: {date}\n\n"
        "Phases are ordered; tasks within a phase are not necessarily. Draw a "
        "dependency edge ONLY where order genuinely matters — an edge that exists "
        "because the work feels sequential blocks work that could have run in "
        "parallel. Name the surfaces each task touches."
    )
    if task_ids == "ordinal":
        decompose_prompt += (
            " Key each task t1, t2, t3, ... in the order you list them, rather than "
            "writing a full id yourself, and write every `needs` entry using those "
            "same t-keys instead of a full slug."
        )
    raw = dict(
        runner.run(
            role="decompose",
            hints=decompose_hints,
            **_decision(source, "decompose", decompose_hints),
            schema=DECOMPOSE_SCHEMA,
            context=context,
            prompt=decompose_prompt,
        )
    )
    # A deterministic rewrite, not a model call. It runs before the adversary,
    # so the adversary sees the split phases.
    decomposition, moves = split_same_phase_needs(
        {**raw, "phases": list(raw.get("phases") or []), "tasks": list(raw.get("tasks") or [])}
    )

    initiative_id = args.get("initiative_id")
    reported_moves = (
        [
            dict(m, task=_prefixed(initiative_id, m["task"]), needs=[_prefixed(initiative_id, n) for n in m["needs"]])
            for m in moves
        ]
        if initiative_id
        else moves
    )

    tasks = [dict(t, needs=list(t.get("needs") or []), surfaces=list(t.get("surfaces") or [])) for t in decomposition.get("tasks") or []]
    if not tasks:
        raise ContractViolation("decompose returned no tasks; there is nothing to propose")

    if task_ids == "ordinal":
        ordinal_problems = _ordinal_needs_problems(tasks)
        if ordinal_problems:
            raise ContractViolation(
                "the decomposed graph used a needs key that names no listed task: "
                + "; ".join(ordinal_problems)
            )

    if initiative_id:
        tasks = [
            dict(t, id=_prefixed(initiative_id, t["id"]), needs=[_prefixed(initiative_id, n) for n in t["needs"]])
            for t in tasks
        ]

    challenge: dict[str, Any] | None = None
    if "review_adversary" in bound:
        challenge = dict(
            runner.run(
                role="review_adversary",
                tier="deep",
                **_decision(source, "review_adversary", None),
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

    tree = list(args.get("tree") or [])
    if tree:
        tasks, surface_failures = _validate_plan_surfaces(tasks, tree)
        if surface_failures:
            raise ContractViolation(
                "plan surface not found in the checkout: " + "; ".join(surface_failures)
            )
        problems = _unresolved_pairs(tasks, tree)
        if problems:
            # An unresolved surface is a decompose defect, never a task to
            # discard: nothing is proposed until every surface names a real
            # path, or the run is quarantined for a human to look at.
            if "review_adversary" not in bound:
                raise ContractViolation(surface_problem(problems))
            unbuildable = dict(
                runner.run(
                    role="review_adversary",
                    tier="deep",
                    **_decision(source, "review_adversary", None),
                    schema=UNBUILDABLE_SCHEMA,
                    context=context,
                    prompt=(
                        "These task surfaces are prose, not real paths in the tree. Resolve "
                        "each to the actual path it means.\n\n" + (surface_problem(problems) or "")
                    ),
                )
            )
            tasks = _apply_corrections(tasks, unbuildable.get("corrections") or [])
            problems = _unresolved_pairs(tasks, tree)
            if problems:
                raise ContractViolation(surface_problem(problems))
        tasks = _split_cross_repo(_apply_surface_resolutions(tasks, tree), tree)

    grants = list(args.get("grants") or [])
    repo_name = str(args.get("repo") or "")
    lint_problems = lint_tickets(tasks, tree, grants, repo_name)
    refusals = [p for p in lint_problems if p.severity == "refusal"]
    if refusals:
        # reach and coupling block the run, same as an unresolved surface does:
        # quarantine outright with no adversary bound, otherwise ask it for a
        # correction and re-lint before trusting the result. The correction
        # schema only rewrites `surfaces` (see _apply_corrections below), so a
        # reach hit sourced from body prose rather than a declared surface has
        # nothing for the correction to match — the wrong belief to hold here
        # is that a correction can reach into prose; it cannot, and the run
        # quarantines on the second lint exactly as it would with no adversary
        # bound at all.
        if "review_adversary" not in bound:
            raise LintRefusal(_lint_refusal_text(refusals))
        correction = dict(
            runner.run(
                role="review_adversary",
                tier="deep",
                **_decision(source, "review_adversary", None),
                schema=UNBUILDABLE_SCHEMA,
                context=context,
                prompt=(
                    "Ticket lint refused these tasks for reach or coupling. Resolve "
                    "each so the problem no longer holds.\n\n" + (_lint_refusal_text(refusals) or "")
                ),
            )
        )
        tasks = _apply_corrections(tasks, correction.get("corrections") or [])
        lint_problems = lint_tickets(tasks, tree, grants, repo_name)
        refusals = [p for p in lint_problems if p.severity == "refusal"]
        if refusals:
            raise LintRefusal(_lint_refusal_text(refusals))

    advisories = [p for p in lint_problems if p.severity == "advisory"]
    if advisories:
        # grant and size never block; they land on the ticket as a `lint:`
        # entry for the human at the gate to see.
        notes: dict[str, list[str]] = {}
        for p in advisories:
            notes.setdefault(p.task, []).append(f"{p.rule}: {p.detail} ({p.fix})")
        tasks = [
            dict(t, lint=[*(t.get("lint") or []), *notes[str(t["id"])]]) if str(t["id"]) in notes else t
            for t in tasks
        ]

    # The seat's own structured output sometimes copies an advisory sentence
    # into `surfaces` after seeing one in a retry prompt (build-output-valid,
    # limit-pause). Same path shape as lifecycle_propose._PATH_TOKEN: a `/` or
    # a file suffix, no spaces, optional trailing `(new)`; an all-caps root
    # file like LICENSE has neither and is kept on that alone. What doesn't
    # look like a path moves to `lint` here, the one place every task passes.
    tasks = [
        dict(
            t,
            surfaces=[s for s in t.get("surfaces") or [] if _looks_like_a_surface(str(s))],
            lint=[
                *(t.get("lint") or []),
                *(f"dropped from surfaces: {s}" for s in t.get("surfaces") or [] if not _looks_like_a_surface(str(s))),
            ],
        )
        for t in tasks
    ]

    # This plan's own task ids are the only valid `needs` targets — a foreign
    # id names real work, just ordered by landing rather than by an edge.
    tasks = _drop_foreign_needs(tasks)

    # Validated before anything is proposed: a typo written the same way in
    # both the task and the plan reaches the work-item arm looking
    # consistent, and the arm rightly refuses to fix it silently. A
    # near-miss that matches more than one declared phase is not corrected
    # either — that is a guess, not a fix — so the node fails here and its
    # existing retry runs.
    phase_order = [str(p.get("id")) for p in decomposition.get("phases") or []]
    resolved_tasks = []
    for task in tasks:
        match = _resolve_phase(str(task.get("phase")), phase_order)
        if match is None:
            raise ContractViolation(
                f"task {task['id']} names phase {task.get('phase')!r}, which is not within "
                f"edit distance {_PHASE_EDIT_DISTANCE_BUDGET} of exactly one declared phase "
                f"({', '.join(phase_order) or 'none declared'})"
            )
        resolved_phase, note = match
        resolved_tasks.append(dict(task, phase=resolved_phase, phase_correction=note) if note else task)
    tasks = resolved_tasks

    shape = epic_shape(
        cartridge,
        phases=len({t["phase"] for t in tasks}),
        tickets=len(tasks),
        repos=_stack_repo_count(tasks, tree),
    )
    landing = landing_for(cartridge, "planned")

    goals = {str(p.get("id")): str(p.get("goal") or "") for p in decomposition.get("phases") or []}
    idea_doc = {"id": initiative_id or run_id, "title": str(idea), "budget_usd": args.get("budget_usd"), "why": str(idea)}
    intake_path = args.get("intake_path")
    initiative_body = initiative_text(idea_doc, phase_order, goals, str(args.get("repo") or ""), intake=intake_path)
    initiative_where = "/".join(part for part in (landing, initiative_id) if part)

    proposals = [
        proposal(
            cartridge,
            kind="item_create",
            target=str(task["id"]),
            evidence=[
                {"check": "phase", "output": str(task.get("phase"))},
                *(
                    [{"check": "phase correction", "output": task["phase_correction"]}]
                    if task.get("phase_correction")
                    else []
                ),
                {"check": "depends on", "output": ", ".join(task["needs"]) or "nothing — can start immediately"},
                {"check": "surfaces", "output": ", ".join(task.get("surfaces") or []) or "none declared"},
                *(
                    [{"check": "adversary on the DAG", "output": "adversary edges applied: " + str(challenge.get("summary"))}]
                    if challenge
                    else []
                ),
                *(
                    [{"check": "lint", "output": "; ".join(task.get("lint") or [])}]
                    if task.get("lint")
                    else []
                ),
                *(
                    {"check": "surface corrected", "output": fix}
                    for fix in task.get("surface_fixes") or []
                ),
                *(
                    {"check": "needs dropped", "output": f"{task['id']}: dropped {dropped!r}, not a task id in this plan"}
                    for dropped in task.get("needs_dropped") or []
                ),
            ],
            rationale=_strip_trailing_tag(str(task.get("body") or decomposition.get("rationale", ""))),
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
    ] + (
        [
            proposal(
                cartridge,
                kind="state_move",
                target=intake_path,
                evidence=[{"check": "initiative", "output": f"{initiative_where}/initiative.md"}],
                rationale="the initiative now exists; the intake file it came from retires out of the queue",
                suggested_action=(
                    f"call `cox route file --from-intake {intake_path}` if that CLI is present; otherwise "
                    f"set {intake_path}'s frontmatter to `initiative: {initiative_id or run_id}` and move it "
                    f"to `intake/done/{intake_path.rsplit('/', 1)[-1]}`"
                ),
            )
        ]
        if intake_path
        else []
    )

    unblocked = [t["id"] for t in tasks if not t["needs"]]
    return {
        "run_id": run_id,
        "date": date,
        "idea": idea,
        "shape": shape,
        "phases": decomposition.get("phases") or [],
        "tasks": sorted(tasks, key=lambda t: str(t["id"])),
        "moves": reported_moves,
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


def _strip_trailing_tag(body: str) -> str:
    """Drop a trailing closing tag on its own line — the seat's own output wrapper, not the ticket."""
    return re.sub(r"\n?</[^\n>]+>\s*$", "", body)


def _item_action(task: Mapping[str, Any], *, landing: str, initiative_id: str | None) -> str:
    """Pure: the create action, carrying every field the work-item arm must write."""
    where = "/".join(part for part in (landing, initiative_id, str(task.get("phase"))) if part)
    # Empty stays `[]`, never a placeholder word: the arm copies this text into
    # frontmatter verbatim, and `needs: [none]` is an edge to a task that does
    # not exist — the third live run landed exactly that and the DAG refused.
    # Ticket lint's grant/size warnings land here too — docs/design/work-shape.md
    # §3 puts them in the ticket's own `lint:` frontmatter list. A retried seat
    # can see the same advisory more than once; dedupe keeps it to one.
    frontmatter = {
        "id": task["id"],
        "title": task.get("title") or task["id"],
        "phase": task.get("phase"),
        "state": "ready",
        "needs": [str(n) for n in task.get("needs") or []],
        "surfaces": [str(x) for x in task.get("surfaces") or []],
        "lint": list(dict.fromkeys(str(x) for x in task.get("lint") or [])),
    }
    # Real YAML, not a hand-quoted guess: the arm copies this block verbatim
    # into the ticket's frontmatter, and a title carrying `: ` broke exactly
    # that the first time nothing here quoted it.
    block = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True, width=1000)
    return f"create {where}/{task['id']}.md with frontmatter\n{block}body = the rationale"


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
        Need("tree", flag="--tree", kind="jsonl_file", required=False,
             help="rows of {repo, path} across the target repo(s); resolves task surfaces to "
                  "real paths and derives the stack (default: surfaces are not resolved)"),
        Need("intake_path", flag="--from-intake", required=False,
             help="path to the intake file this idea came from, when it did; links initiative.md "
                  "and the intake file both ways"),
        Need("grants", flag="--grants", kind="json_file", required=False,
             help="JSON array of commands the build seat's sandbox actually permits, for the "
                  "ticket-lint grant rule (default: none granted)"),
    ),
)
