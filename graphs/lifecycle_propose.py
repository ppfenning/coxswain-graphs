"""lifecycle-propose — the development loop.

    plan -> build (worktree) -> review -> emit

Takes one ticket, produces reviewed work and proposals. Nothing is pushed,
opened, or merged. The build node returns a patch; applying it is the shell's
job, inside a worktree the shell owns.

Deferred from v0 (see graphs/lifecycle-propose.md): intake queue, epic-threshold
scoping, the adversarial reviewer pair, arbitration, the bounded fix loop,
verification, retro.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from graphs._contract import proposal, require, require_cartridge
from runner.protocol import NodeRunner

__all__ = ["run", "GRAPH_NAME"]

GRAPH_NAME = "lifecycle-propose"

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

    review = runner.run(
        role="review_charter",
        tier="deep",
        schema=REVIEW_SCHEMA,
        context=context,
        prompt=(
            "Review this change against the team's own written charter in your "
            f"context.\n\nTicket: {ticket}\nSummary: {build.get('summary')}\n"
            f"Change facts: {facts}\nPatch:\n{build.get('patch')}\n\n"
            "Cite the charter principle behind every finding."
        ),
    )

    proposals: list[dict[str, Any]] = []
    if review.get("verdict") == "approve":
        # A draft PR has no effect until someone opens it, which is why it is the
        # one kind that starts eligible. It is still emitted, never executed.
        proposals.append(
            proposal(
                cartridge,
                kind="draft_pr_create",
                target=str(ticket),
                evidence=[
                    {"check": "review_charter verdict", "output": "approve"},
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
        "plan": dict(plan),
        "build": dict(build),
        "review": dict(review),
        "change_facts": facts,
        "proposals": proposals,
    }
