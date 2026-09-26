"""rescue-review — the review half of the development loop, started from a patch that already exists.

handoff -> review_charter -> review_adversary -> [arbitrate] -> emit

Takes a ticket, a patch and the evidence rows the harness produced by running
the checks, and returns a verdict shaped like a lifecycle result so the driver
reads it the same way. There is no plan node, no build node and no fix loop:
one review round, and the verdict is final.

Every stage is lifecycle-propose's own. The evidence rows fill the slot the
build's `commands_run` fills there, so reviewers meet them under the same
sentence that says the harness ran them. There is no builder to ask, so the
ticket text tells the reviewers to judge the patch and the rows as they stand.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from graphs._contract import proposal, require, require_cartridge, review_tier
from graphs._spec import GraphSpec, Need
from graphs.delivery.lifecycle_propose import (
    _change_facts,
    _file_chunks,
    _handoff,
    _handoff_critique,
    _infra_result,
    _NodeFailure,
    _patch_truncated_review,
    _review_round,
    _ticket_text,
    patch_parses,
    round_summary,
)
from runner.protocol import NodeRunner

__all__ = ["GRAPH_NAME", "SPEC", "run"]

GRAPH_NAME = "rescue-review"

NO_BUILDER = (
    "There is no builder in this run. The patch and the harness_verify rows are all the evidence "
    "there will be: judge them as they stand, and do not ask for a rerun or for anything to be run again."
)


def _approval_evidence(
    *,
    tier: int,
    review: Mapping[str, Any],
    adversary: Mapping[str, Any] | None,
    arbitration: Mapping[str, Any] | str | None,
    facts: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """The evidence rows of a draft_pr_create proposal, named as lifecycle-propose names them."""
    return [
        {"check": "review tier", "output": str(tier)},
        {"check": "review_charter verdict", "output": str(review.get("verdict"))},
        *(
            [
                {"check": "adversary verdict", "output": str(adversary.get("verdict"))},
                {"check": "strongest objection", "output": str(adversary.get("strongest_objection"))},
            ]
            if adversary
            else []
        ),
        *(
            [{"check": "arbitration", "output": f"{arbitration.get('sided_with')}: {arbitration.get('reasoning')}"}]
            if isinstance(arbitration, Mapping) and arbitration
            else [{"check": "arbitration", "output": arbitration}]
            if arbitration
            else []
        ),
        {"check": "changed lines", "output": str(facts["changed_lines"])},
        *({"check": row.get("command"), "output": row.get("output")} for row in rows),
    ]


def _result(
    args: Mapping[str, Any],
    *,
    tier: int | None,
    handoff: Mapping[str, Any] | None,
    review: Mapping[str, Any],
    verdict: str,
    build: Mapping[str, Any],
    facts: Mapping[str, Any],
    source: str,
    adversary: Mapping[str, Any] | None = None,
    arbitration: Mapping[str, Any] | str | None = None,
    quarantined: bool = False,
    placeholder: bool = False,
    proposals: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The lifecycle result's keys, with the plan-side ones empty."""
    stopped = (
        None if verdict == "approve" else "harness fault: review placeholders" if quarantined else "rescue_revise"
    )
    return {
        "run_id": args["run_id"],
        "date": args["date"],
        "ticket": args["ticket"],
        "scope": None,
        "review_tier": tier,
        "handoff": handoff,
        "adversary": adversary,
        "arbitration": arbitration,
        "plan": None,
        "plan_competition": None,
        "plan_attack": None,
        "plan_gate": None,
        "build": dict(build),
        "review": dict(review),
        "change_facts": facts,
        "verdict": verdict,
        "fix_loop": {
            "attempts": 1,
            "stopped": stopped,
            "continuations": 0,
            "rounds": [round_summary(1, source, review, adversary, arbitration, verdict)],
            **({"review_placeholder": True} if placeholder else {}),
        },
        "proposals": proposals or [],
    }


def run(args: Mapping[str, Any], runner: NodeRunner) -> dict[str, Any]:
    """Run the graph: one review round over a patch that already exists."""
    cartridge = require_cartridge(args)
    _, _, ticket, patch = require(args, "run_id", "date", "ticket", "patch")
    ticket_text = "\n".join([_ticket_text(ticket, args.get("ticket_title"), args.get("ticket_body")), NO_BUILDER])
    rows = [{"source": "harness_verify", **row} for row in args.get("evidence") or [] if isinstance(row, Mapping)]
    build = {
        "patch": patch,
        "summary": "a patch that already exists, reviewed with the harness's own check results",
        "files_touched": [path for path, _ in _file_chunks(patch)],
        "commands_run": rows,
    }
    facts = _change_facts(build)
    context = list(cartridge.get("context") or [])
    bound = cartridge.get("skills") or {}

    # A patch that does not parse is not worth two reviewers; nothing is asked of a model.
    unusable = patch_parses(patch) if patch.strip() else "the patch is empty"
    if unusable is not None:
        return _result(
            args, tier=None, handoff=None, review=_patch_truncated_review(unusable), verdict="revise",
            build=build, facts=facts, source="review",
        )

    tier = review_tier(
        cartridge, change_facts=facts, surfaces=list(args.get("surfaces") or []), patterns=list(args.get("patterns") or [])
    )
    handoff = None
    try:
        if "handoff" in bound:
            handoff = _handoff(runner, context=context, ticket=ticket_text, plan={}, build=build, facts=facts, ticket_id=ticket)
        if handoff is not None and not handoff.get("complete"):
            review, _, _, verdict, _, _ = _handoff_critique(handoff)
            return _result(
                args, tier=tier, handoff=handoff, review=review, verdict=verdict,
                build=build, facts=facts, source="handoff",
            )
        review, adversary, arbitration, verdict, placeholder, quarantined = _review_round(
            runner, context=context, bound=bound, ticket=ticket_text, build=build, facts=facts,
            handoff=handoff, tier=tier, attempt=1, task_id=ticket,
        )
    except _NodeFailure as exc:
        return _infra_result(
            run_id=args["run_id"], date=args["date"], ticket=ticket, scope=None, build=build, handoff=handoff, exc=exc
        )

    approved = (
        [
            proposal(
                cartridge,
                kind="draft_pr_create",
                target=str(ticket),
                evidence=_approval_evidence(
                    tier=tier, review=review, adversary=adversary, arbitration=arbitration, facts=facts, rows=rows
                ),
                rationale=review.get("rationale", ""),
                suggested_action=f"open a draft PR for {ticket} from the reviewed patch",
            )
        ]
        if verdict == "approve"
        else []
    )
    return _result(
        args, tier=tier, handoff=handoff, review=review, adversary=adversary, arbitration=arbitration,
        verdict=verdict, quarantined=quarantined, placeholder=placeholder, build=build, facts=facts,
        source="review", proposals=approved,
    )


SPEC = GraphSpec(
    name="rescue-review",
    graph_name=GRAPH_NAME,
    run=run,
    summary="review a patch that already exists against the harness's own check results — verdict and proposals out",
    needs=(
        Need("ticket", flag="--ticket", help="the ticket the patch was written for"),
        Need("ticket_body", flag="--ticket-body", kind="text_or_path", required=False, help="the ticket body, or a path to it"),
        Need("patch", flag="--patch", kind="text_or_path", help="the patch to review, or a path to it"),
        Need("evidence", flag="--evidence", kind="jsonl_file", required=False,
             help="JSON-Lines rows of {command, output, source: harness_verify} from the harness's checks"),
    ),
)
