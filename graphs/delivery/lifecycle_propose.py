"""lifecycle-propose — the development loop for ONE task.

    scope -> plan -> build (worktree) -> handoff -> review -> adversary
          -> arbitrate -> emit

Takes one task, produces reviewed work and proposals. Nothing is pushed, opened,
or merged. The build node returns a patch; applying it is the shell's job,
inside a worktree the shell owns.

Two convictions shape the back half of this graph.

**Nothing is one-shot.** Every change gets a reviewer, and how many it gets is
proportional to what a mistake would cost — `review_tier` decides, not the
author. A dangerous surface earns an adversary and an arbitrator even when the
diff is four lines.

**A step never builds on an unvalidated handoff.** The `handoff` node checks
that what build produced actually satisfies what review needs before review sees
it, and REFUSES rather than passing a gap along. A phase that goes quietly wrong
usually did so three steps earlier.

Every node after `build` is an optional role: a team that binds none of them
gets the original single-reviewer loop, which is what optional means.

Deferred (see graphs/lifecycle-propose.md): intake queue, the bounded fix loop,
verification, retro.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from graphs._contract import (
    ContractViolation,
    epic_shape,
    landing_for,
    proposal,
    require,
    require_cartridge,
    review_tier,
)
from runner.protocol import NodeRunner

__all__ = ["run", "GRAPH_NAME"]

GRAPH_NAME = "lifecycle-propose"

SCOPE_SCHEMA = {
    "type": "object",
    "properties": {
        "phases": {"type": "array", "items": {"type": "string"}},
        "tickets": {"type": "array", "items": {"type": "string"}},
        "repos": {"type": "array", "items": {"type": "string"}},
        "state": {"type": "string", "enum": ["active", "planned", "future"]},
        "parent_epic": {"type": "string", "description": "existing epic to attach to, or empty"},
        "rationale": {"type": "string"},
    },
    "required": ["phases", "tickets", "repos", "state", "parent_epic", "rationale"],
    "additionalProperties": False,
}

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "steps": {"type": "array", "items": {"type": "string"}},
        "files_expected": {"type": "array", "items": {"type": "string"}},
        "out_of_scope": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["steps", "files_expected", "out_of_scope"],
    "additionalProperties": False,
}

BUILD_SCHEMA = {
    "type": "object",
    "properties": {
        "patch": {"type": "string", "description": "unified diff, applied by the shell in its own worktree"},
        "summary": {"type": "string"},
        "files_touched": {"type": "array", "items": {"type": "string"}},
        "commands_run": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"command": {"type": "string"}, "output": {"type": "string"}},
                "required": ["command", "output"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["patch", "summary", "files_touched", "commands_run"],
    "additionalProperties": False,
}

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "revise", "reject"]},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "charter_principle": {"type": "string"},
                    "detail": {"type": "string"},
                    "file": {"type": "string"},
                },
                "required": ["charter_principle", "detail", "file"],
                "additionalProperties": False,
            },
        },
        "rationale": {"type": "string"},
    },
    "required": ["verdict", "findings", "rationale"],
    "additionalProperties": False,
}


HANDOFF_SCHEMA = {
    "type": "object",
    "properties": {
        "complete": {"type": "boolean"},
        "missing": {"type": "array", "items": {"type": "string"}},
        "brief": {"type": "string", "description": "the small thing the next step actually needs"},
    },
    "required": ["complete", "missing", "brief"],
    "additionalProperties": False,
}

ADVERSARY_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "revise", "reject"]},
        "objections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"claim": {"type": "string"}, "why_wrong": {"type": "string"}},
                "required": ["claim", "why_wrong"],
                "additionalProperties": False,
            },
        },
        "strongest_objection": {"type": "string"},
    },
    "required": ["verdict", "objections", "strongest_objection"],
    "additionalProperties": False,
}

ARBITRATE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "revise", "reject"]},
        "sided_with": {"type": "string", "enum": ["charter", "adversary", "neither"]},
        "reasoning": {"type": "string"},
    },
    "required": ["verdict", "sided_with", "reasoning"],
    "additionalProperties": False,
}


def _change_facts(build: Mapping[str, Any]) -> dict[str, Any]:
    """Deterministic facts about the change, for the reviewer and the gate.

    Counted from the patch rather than asked of the model: a node reporting its
    own diff size is reporting a recollection, and the review tier keys off
    these numbers.
    """
    patch = build.get("patch") or ""
    lines = patch.splitlines()
    added = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
    files = list(build.get("files_touched") or [])
    return {
        "files_touched": files,
        "module_count": len({f.rsplit("/", 1)[0] for f in files}),
        "added_lines": added,
        "removed_lines": removed,
        "changed_lines": added + removed,
    }


def run(args: Mapping[str, Any], runner: NodeRunner) -> dict[str, Any]:
    """Run the graph. Every input arrives as an argument — no clock, no disk."""
    cartridge = require_cartridge(args)
    run_id, date, ticket = require(args, "run_id", "date", "ticket")

    context = list(cartridge.get("context") or [])
    proposals: list[dict[str, Any]] = []

    # Scoping is a SEPARATE ACT from filing, and it runs first: unscoped work
    # routes to the future-work landing area, never onto the active board.
    # Optional — a team that has not bound `scope_epic` simply does not get it,
    # which is what an optional role means.
    scope: dict[str, Any] | None = None
    if "scope_epic" in (cartridge.get("skills") or {}):
        scope = dict(
            runner.run(
                role="scope_epic",
                tier="standard",
                schema=SCOPE_SCHEMA,
                context=context,
                prompt=(
                    f"Scope this work.\n\nTicket: {ticket}\nDate: {date}\n\n"
                    "List the phases, the tickets, and the repositories it touches. "
                    "Say whether it is being worked now (active), scoped and scheduled "
                    "(planned), or roadmapped for later (future). Name an existing epic "
                    "to attach to if one covers this area."
                ),
            )
        )
        shape = epic_shape(
            cartridge,
            phases=len(scope.get("phases") or []),
            tickets=len(scope.get("tickets") or []),
            repos=len(scope.get("repos") or []),
        )
        landing = landing_for(cartridge, scope.get("state", "planned"))
        scope["shape"] = shape
        scope["landing"] = landing

        proposals.append(
            proposal(
                cartridge,
                kind="item_create",
                target=str(scope.get("parent_epic") or ticket),
                evidence=[
                    {"check": "epic_threshold", "output": f"{shape} ({len(scope.get('tickets') or [])} tickets, {len(scope.get('phases') or [])} phases, {len(scope.get('repos') or [])} repos)"},
                    {"check": "work_routing", "output": f"state '{scope.get('state')}' lands in {landing}"},
                ],
                rationale=str(scope.get("rationale", "")),
                suggested_action=(
                    f"file as {shape} in {landing}"
                    + (f", attached to {scope['parent_epic']}" if scope.get("parent_epic") else "")
                ),
            )
        )

    plan = runner.run(
        role="plan",
        tier="standard",
        schema=PLAN_SCHEMA,
        context=context,
        prompt=(
            f"Decompose this ticket into an ordered plan.\n\nTicket: {ticket}\n"
            f"Date: {date}\n\nName the files you expect to touch, and state what is "
            "explicitly out of scope."
        ),
    )

    build = runner.run(
        role="build",
        tier="standard",
        schema=BUILD_SCHEMA,
        context=context,
        prompt=(
            f"Carry out this plan and return the change as a unified diff.\n\n"
            f"Ticket: {ticket}\nPlan: {plan}\n\nReturn the patch only — it is applied "
            "by the shell into a worktree, never by you. Include the deterministic "
            "commands you ran and their output."
        ),
    )

    facts = _change_facts(build)
    bound = cartridge.get("skills") or {}
    tier = review_tier(
        cartridge,
        change_facts=facts,
        surfaces=list(args.get("surfaces") or []),
        patterns=list(args.get("patterns") or []),
    )

    # The shuttle. Between build and review, someone checks that what came out
    # of the last step is actually what the next one needs — and stops here if
    # it is not. A review of a half-finished change produces a confident opinion
    # about the wrong thing.
    handoff: dict[str, Any] | None = None
    if "handoff" in bound:
        handoff = dict(
            runner.run(
                role="handoff",
                tier="standard",
                schema=HANDOFF_SCHEMA,
                context=context,
                prompt=(
                    "The build step is done and the review step is next. Does what "
                    "build produced actually contain what a reviewer needs?\n\n"
                    f"Task: {ticket}\nPlan: {plan}\nSummary: {build.get('summary')}\n"
                    f"Files: {build.get('files_touched')}\nChange facts: {facts}\n\n"
                    "List anything missing, and compress the rest into the smallest "
                    "brief that lets review start."
                ),
            )
        )
        if not handoff.get("complete"):
            missing = ", ".join(handoff.get("missing") or []) or "unspecified"
            raise ContractViolation(
                f"handoff from build to review is incomplete for '{ticket}': {missing}. "
                "The graph stops rather than reviewing a change that is not finished — "
                "a step that builds on a gap is how a phase goes quietly wrong."
            )

    review = runner.run(
        role="review_charter",
        tier="deep",
        schema=REVIEW_SCHEMA,
        context=context,
        prompt=(
            "Review this change against the team's own written charter in your "
            f"context.\n\nTask: {ticket}\nSummary: {build.get('summary')}\n"
            f"Change facts: {facts}\n"
            + (f"Handoff brief: {handoff.get('brief')}\n" if handoff else "")
            + f"Patch:\n{build.get('patch')}\n\n"
            "Cite the charter principle behind every finding."
        ),
    )

    # Tier 0 is the cheapest review, never the absence of one.
    adversary: dict[str, Any] | None = None
    if tier >= 1 and "review_adversary" in bound:
        adversary = dict(
            runner.run(
                role="review_adversary",
                tier="deep",
                schema=ADVERSARY_SCHEMA,
                context=context,
                prompt=(
                    "Your job is to disagree. Find what this change gets wrong, and "
                    "what the first reviewer accepted too easily.\n\n"
                    f"Task: {ticket}\nChange facts: {facts}\n"
                    f"First reviewer said: {review.get('verdict')} — {review.get('rationale')}\n"
                    f"Patch:\n{build.get('patch')}\n\n"
                    "State your strongest objection plainly, even if you end up approving."
                ),
            )
        )

    # Arbitration on disagreement, and unconditionally at tier 2 — where the
    # cost of being wrong is high enough that agreement between two reviewers
    # is not by itself sufficient reason to believe them.
    arbitration: dict[str, Any] | None = None
    disagreed = adversary is not None and adversary.get("verdict") != review.get("verdict")
    if "arbitrate" in bound and adversary is not None and (disagreed or tier == 2):
        arbitration = dict(
            runner.run(
                role="arbitrate",
                tier="deep",
                schema=ARBITRATE_SCHEMA,
                context=context,
                prompt=(
                    "Two reviewers have looked at this change. Decide.\n\n"
                    f"Task: {ticket}\nReview tier: {tier}\n"
                    f"Charter reviewer: {review.get('verdict')} — {review.get('rationale')}\n"
                    f"Adversary: {adversary.get('verdict')} — {adversary.get('strongest_objection')}\n"
                    f"Change facts: {facts}\n\n"
                    "Say who you sided with and why. 'neither' is allowed."
                ),
            )
        )

    # The last word: arbitration if it ran, otherwise both reviewers must agree.
    # Silence from an unbound optional role is not an approval, but neither is it
    # an objection — an unbound adversary simply leaves the charter reviewer
    # deciding, exactly as before.
    if arbitration is not None:
        verdict = arbitration.get("verdict")
    elif adversary is not None:
        verdict = "approve" if review.get("verdict") == adversary.get("verdict") == "approve" else "revise"
    else:
        verdict = review.get("verdict")

    if verdict == "approve":
        # A draft PR has no effect until someone opens it, which is why it is the
        # one kind that starts eligible. It is still emitted, never executed.
        proposals.append(
            proposal(
                cartridge,
                kind="draft_pr_create",
                target=str(ticket),
                evidence=[
                    {"check": "review tier", "output": str(tier)},
                    {"check": "review_charter verdict", "output": str(review.get("verdict"))},
                    *(
                        [{"check": "adversary verdict", "output": str(adversary.get("verdict"))},
                         {"check": "strongest objection", "output": str(adversary.get("strongest_objection"))}]
                        if adversary
                        else []
                    ),
                    *(
                        [{"check": "arbitration", "output": f"{arbitration.get('sided_with')}: {arbitration.get('reasoning')}"}]
                        if arbitration
                        else []
                    ),
                    {"check": "changed lines", "output": str(facts["changed_lines"])},
                    # Normalised into the evidence shape rather than spread raw:
                    # a commands_run entry is keyed `command`, and everything
                    # downstream — the gate, the manifest — reads `check`.
                    *(
                        {"check": entry.get("command"), "output": entry.get("output")}
                        for entry in build.get("commands_run", [])
                        if isinstance(entry, Mapping)
                    ),
                ],
                rationale=review.get("rationale", ""),
                suggested_action=f"open a draft PR for {ticket} from the build worktree",
            )
        )

    return {
        "run_id": run_id,
        "date": date,
        "ticket": ticket,
        "scope": scope,
        "review_tier": tier,
        "handoff": handoff,
        "adversary": adversary,
        "arbitration": arbitration,
        "plan": dict(plan),
        "build": dict(build),
        "review": dict(review),
        "change_facts": facts,
        "proposals": proposals,
    }


from graphs._spec import GraphSpec, Need  # noqa: E402

SPEC = GraphSpec(
    name="lifecycle",
    graph_name=GRAPH_NAME,
    run=run,
    summary="the development loop: scope, plan, build, review — proposals out, nothing pushed",
    needs=(
        Need("ticket", flag="--ticket", help="the ticket to work"),
    ),
)
