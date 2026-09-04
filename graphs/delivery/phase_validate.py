"""phase-validate — did these tasks, together, accomplish the phase's goal?

    validate_chunk (per task) -> validate_phase (once)

The two validators the epic-swarm spec asks for, as a graph. They are model
calls, and model calls belong in graphs — a driver that called the runner
directly would be the first non-graph thing in the system to do so, and every
purity rule the portability suite enforces would stop applying to the two nodes
whose judgment the phase boundary rests on. So the driver invokes this the way
it invokes any other graph, through `invoke_graphs`, and gets back an opinion
rather than a decision.

`validate_chunk` asks whether one task satisfied its own description. It is
cheap, it runs per task, and it is largely a restatement of the `done_criteria`
the cartridge already carries.

`validate_phase` is the one that earns its place. It reads **the phase's
original goal** — not the task list, the goal — and asks whether what now exists
accomplishes it. Five tasks that each went green and do not add up is a real
failure mode, invisible to every check below it: each task passed its own
review, each draft is defensible, and the phase is nonetheless not done.

**A validator is never the phase's owner.** That is not a note in a prompt, it
is the shape of the input: `phase_state` carries machine evidence, change facts
and ANOTHER instance's review verdicts, and this graph strips a builder's own
`summary` off every task before a prompt is built. A summary is the owner's
account of its own work, and a validator handed one is reviewing a recollection.

Advisory by construction: this graph emits **no proposals**. It says what it
found; the driver decides what that costs.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from graphs._contract import ContractViolation, require, require_cartridge
from runner.protocol import NodeRunner

__all__ = ["run", "GRAPH_NAME"]

GRAPH_NAME = "phase-validate"

VALIDATE_CHUNK_SCHEMA = {
    "type": "object",
    "properties": {
        "satisfied": {"type": "boolean"},
        "gaps": {"type": "array", "items": {"type": "string"}},
        "reasoning": {"type": "string"},
    },
    "required": ["satisfied", "gaps", "reasoning"],
    "additionalProperties": False,
}

VALIDATE_PHASE_SCHEMA = {
    "type": "object",
    "properties": {
        "goal_met": {"type": "boolean"},
        "partial": {"type": "boolean"},
        "missing": {"type": "array", "items": {"type": "string"}},
        "quarantine_blocks_dependents": {"type": "boolean"},
        "reasoning": {"type": "string"},
    },
    "required": ["goal_met", "partial", "missing", "quarantine_blocks_dependents", "reasoning"],
    "additionalProperties": False,
}

# What a task entry may carry into a prompt. `summary` is absent on purpose and
# actively dropped below: it is the builder's own account of its own change, and
# a validator that reads it is being told the answer by the party under review.
# `patch` IS on the list: the diff git applied is machine evidence, not the
# builder's account of it. A validator without it reported, in its own words,
# that it could not verify anything and refused a task the reviewers approved.
_TASK_FIELDS = ("id", "title", "description", "evidence", "change_facts", "review_verdict", "patch")


def _task_view(task: Mapping[str, Any]) -> dict[str, Any]:
    """One task as the validators may see it: evidence, facts, others' verdicts.

    Whitelisted rather than blacklisted. A denylist that drops `summary` today
    lets `builder_notes` through tomorrow, and the property being defended —
    that no validator reads the owner's account of its own work — deserves the
    failure direction where an unknown field is invisible rather than trusted.
    """
    return {field: task.get(field) for field in _TASK_FIELDS if field in task}


# Phrases a verdict uses about ITS OWN making, not about the code under it. A
# validator that says it will read the files later has not judged anything; the
# structured output is a note to self that the schema happened to accept. Each
# marker describes the author's process — "the patch leaves a placeholder
# function" matches none of them, and must not, because that is a finding.
_PLACEHOLDER_MARKERS = (
    "placeholder pending",
    "pending verification",
    "pending file read",
    "will follow up",
    "will verify",
    "before finalizing",
    "provisional verdict",
)


def _is_placeholder(verdict: Mapping[str, Any]) -> bool:
    """Pure: does this verdict describe itself as not yet made?

    Run 17 lost an approved task to exactly this. `route status` passed charter
    review, the adversary's revise was overturned at arbitration, and the phase
    validator said the goal was met — and then `validate_chunk` returned, as its
    final structured output, `satisfied: false` with the reasoning "Placeholder
    pending verification via file reads; will follow up with tool calls before
    finalizing." Two turns; it had read nothing. The task was quarantined and
    recovered by hand with the suite green.

    The gaps are searched as well as the reasoning: that verdict's single gap
    ended "pending file read", which is the same admission in the other field.
    """
    text = " ".join(
        [
            str(verdict.get("reasoning") or ""),
            *(str(item) for item in verdict.get("gaps") or []),
            *(str(item) for item in verdict.get("missing") or []),
        ]
    ).lower()
    return any(marker in text for marker in _PLACEHOLDER_MARKERS)


def _verdict(
    runner: NodeRunner,
    *,
    role: str,
    tier: str,
    schema: Mapping[str, Any],
    context: list[str],
    prompt: str,
    subject: str,
) -> dict[str, Any]:
    """One verdict, and one retry if the node returns a note to itself instead.

    Retried ONCE and never more. A validator that will not answer twice is a
    fault in the harness or the skill, and grinding it until something parses
    would be manufacturing a verdict — the graph raises instead, so the record
    says the validator produced no judgment rather than blaming the task.

    The retry is not cheaper and not easier: same role, same tier, same prompt,
    plus the plain statement that this is the last ask. Asking again more gently
    would be asking a different question.
    """
    first = dict(runner.run(role=role, tier=tier, schema=schema, context=context, prompt=prompt))
    if not _is_placeholder(first):
        return first

    second = dict(
        runner.run(
            role=role,
            tier=tier,
            schema=schema,
            context=context,
            prompt=(
                f"{prompt}\n\n"
                "Your previous answer described itself as provisional — a placeholder, "
                "or work you would follow up with later. There is no later: this call "
                "is the verdict, and nothing runs after it to finish the thought. "
                "Judge what is in front of you now, with the tools you have now, and "
                "return a verdict you are willing to stand behind. If the evidence does "
                "not let you decide, say that as the verdict and name what would."
            ),
        )
    )
    if _is_placeholder(second):
        raise ContractViolation(
            f"the '{role}' node returned a placeholder rather than a verdict for '{subject}', "
            f"twice: {str(second.get('reasoning') or '')[:200]!r}. The graph refuses to treat "
            "a note-to-self as a judgment — a task quarantined on this is quarantined by a "
            "harness fault, not on its own merits."
        )
    return second


def run(args: Mapping[str, Any], runner: NodeRunner) -> dict[str, Any]:
    """Run the graph. The phase's state arrives as an argument; nothing is read."""
    cartridge = require_cartridge(args)
    run_id, date, phase_state = require(args, "run_id", "date", "phase_state")

    bound = cartridge.get("skills") or {}
    if "validate_phase" not in bound:
        raise ContractViolation(
            "this graph needs the optional role 'validate_phase' bound in the cartridge; "
            "a team that has not bound it gets no claim about phase completion, which is "
            "honest — but it is the driver's job to notice that before invoking this"
        )

    context = list(cartridge.get("context") or [])
    phase = dict(phase_state.get("phase") or {})
    tasks = sorted(
        (dict(t) for t in phase_state.get("tasks") or []),
        key=lambda t: str(t.get("id")),
    )
    quarantined = [dict(q) for q in phase_state.get("quarantined") or []]

    # Per task, and only where the role is bound. An unbound `validate_chunk`
    # leaves this list empty rather than fabricating an opinion per task.
    chunk_verdicts: list[dict[str, Any]] = []
    if "validate_chunk" in bound:
        for task in tasks:
            verdict = _verdict(
                runner,
                role="validate_chunk",
                tier="standard",
                schema=VALIDATE_CHUNK_SCHEMA,
                context=context,
                subject=str(task.get("id")),
                prompt=(
                    "Did this task actually satisfy its own description?\n\n"
                    f"Date: {date}\nPhase: {phase.get('id')}\n"
                    f"Task: {_task_view(task)}\n\n"
                    "Judge the machine evidence and the change facts, not anyone's "
                    "account of the work. Name every gap you find; an empty list "
                    "means you found none, not that you did not look."
                ),
            )
            chunk_verdicts.append(
                {
                    "task": str(task.get("id")),
                    "satisfied": bool(verdict.get("satisfied")),
                    "gaps": list(verdict.get("gaps") or []),
                    "reasoning": str(verdict.get("reasoning", "")),
                }
            )

    phase_verdict = _verdict(
        runner,
        role="validate_phase",
        tier="deep",
        schema=VALIDATE_PHASE_SCHEMA,
        context=context,
        subject=str(phase.get("id")),
        prompt=(
            "THE PHASE'S GOAL, which is what you are judging against:\n"
            f"{phase.get('goal')}\n\n"
            f"Phase: {phase.get('id')}\nDate: {date}\n\n"
            f"What now exists, per task: {[_task_view(t) for t in tasks]}\n"
            f"Per-task verdicts from an independent chunk validator: {chunk_verdicts}\n"
            f"Quarantined tasks, with the diagnosis that put them there: {quarantined}\n\n"
            "Five individually-green tasks that do not add up is the failure you "
            "exist to catch. Each one passed its own review; the phase can still be "
            "unfinished, and nothing below you can see that.\n\n"
            "A quarantined task is a fact, not an absence: say whether what it left "
            "undone blocks the work that depends on this phase. Say plainly what is "
            "missing, and do not treat 'every task finished' as the goal being met."
        ),
    )

    return {
        "run_id": run_id,
        "date": date,
        "phase": phase.get("id"),
        "chunk_verdicts": chunk_verdicts,
        "phase_verdict": {
            "goal_met": bool(phase_verdict.get("goal_met")),
            "partial": bool(phase_verdict.get("partial")),
            "missing": list(phase_verdict.get("missing") or []),
            "quarantine_blocks_dependents": bool(phase_verdict.get("quarantine_blocks_dependents")),
            "reasoning": str(phase_verdict.get("reasoning", "")),
        },
        # No proposals, deliberately. This graph reports; the driver acts. A
        # validator that proposed its own remedy would be grading its own paper
        # at the gate.
        "proposals": [],
    }


from graphs._spec import GraphSpec, Need  # noqa: E402

SPEC = GraphSpec(
    name="validate",
    graph_name=GRAPH_NAME,
    run=run,
    summary="did these tasks, together, accomplish the phase's goal — judged on evidence, never on the builder's summary",
    needs=(
        Need("phase_state", flag="--phase-state", kind="json_file",
             help="phase goal, task evidence, and quarantine list to validate"),
    ),
)
