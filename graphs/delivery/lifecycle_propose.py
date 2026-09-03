"""lifecycle-propose — the development loop for ONE task.

    scope -> plan -> build (worktree) -> handoff -> review -> adversary
          -> arbitrate -> emit

Takes one task, produces reviewed work and proposals. Nothing is pushed, opened,
or merged. The build node returns a patch; applying it is the shell's job,
inside a worktree the shell owns.

Three convictions shape the back half of this graph.

**Nothing is one-shot.** Every change gets a reviewer, and how many it gets is
proportional to what a mistake would cost — `review_tier` decides, not the
author. A dangerous surface earns an adversary and an arbitrator even when the
diff is four lines.

**A step never builds on an unvalidated handoff.** The `handoff` node checks
that what build produced actually satisfies what review needs before review sees
it, and REFUSES rather than passing a gap along. A phase that goes quietly wrong
usually did so three steps earlier.

**A fix loop must never launder struggle into trust.** Sending a rejected change
back to the builder is ordinary; forgetting that it was sent back is not. The
loop counts its attempts and carries the count out on the proposal, so a task
that passed on the third try stays distinguishable, everywhere downstream, from
one that passed clean. The ledger is what refuses to let a repeated-attempt pass
extend a streak — but it can only refuse what it can see, and this graph is the
only place that knows. A graph that quietly retried until something passed would
be manufacturing exactly the clean record the ledger exists to disbelieve.

Every node after `build` is an optional role: a team that binds none of them
gets the original single-reviewer loop, which is what optional means.

Deferred (see graphs/lifecycle-propose.md): intake queue, verification, retro.
"""

from __future__ import annotations

from collections.abc import Mapping
from difflib import SequenceMatcher
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


DEFAULT_FIX_ATTEMPTS = 2


def _ticket_text(ticket: Any, title: Any, body: Any) -> str:
    """Pure: the id, then the title and body when the harness supplied them."""
    parts = [str(ticket)]
    if title:
        parts[0] = f"{ticket} — {title}"
    if body:
        parts.append(str(body).strip())
    return "\n".join(parts)

# How much of a patch the handoff sees: all of it, up to a bound that only a
# pathological diff reaches. A 6,000-character preview was tried first and the
# handoff — correctly — refused every patch it could see was cut off. The
# shuttle judges the cargo; it cannot judge half of it.
PATCH_PREVIEW_CHARS = 200_000

# Two successive patches this similar are the same patch with the whitespace
# moved. 0.98 rather than 1.0 because a builder that re-emits its own diff
# rarely re-emits it byte-identically, and "it changed a comment" is not the
# objection falling.
NO_PROGRESS_RATIO = 0.98


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


def _claims(adversary: Mapping[str, Any] | None) -> set[str]:
    """The adversary's objections, normalised for comparison across rounds.

    Case and surrounding whitespace are not the objection. The same complaint
    typed differently in the next round is still the same complaint, still
    standing — and a comparison strict enough to miss that would let a loop
    re-litigate one objection until the cap ran out.
    """
    if not adversary:
        return set()
    return {
        str(objection.get("claim") or "").strip().lower()
        for objection in adversary.get("objections") or []
        if isinstance(objection, Mapping) and str(objection.get("claim") or "").strip()
    }


def _critique(
    review: Mapping[str, Any],
    adversary: Mapping[str, Any] | None,
    arbitration: Mapping[str, Any] | None,
) -> str:
    """Everything the reviewers held against the change, as one block of text.

    The whole critique, not a summary of it. A builder handed "review asked for
    changes" will fix the thing it already thought was wrong; a builder handed
    the objection verbatim has to answer that objection.
    """
    lines = [f"Charter reviewer: {review.get('verdict')} — {review.get('rationale')}"]
    lines += [
        f"- finding ({finding.get('charter_principle')}) in {finding.get('file')}: {finding.get('detail')}"
        for finding in review.get("findings") or []
        if isinstance(finding, Mapping)
    ]
    if adversary is not None:
        lines.append(f"Adversary: {adversary.get('verdict')} — strongest: {adversary.get('strongest_objection')}")
        lines += [
            f"- objection: {objection.get('claim')} — {objection.get('why_wrong')}"
            for objection in adversary.get("objections") or []
            if isinstance(objection, Mapping)
        ]
    if arbitration is not None:
        lines.append(f"Arbitration sided with {arbitration.get('sided_with')}: {arbitration.get('reasoning')}")
    return "\n".join(lines)


def _handoff(
    runner: NodeRunner,
    *,
    context: list[str],
    ticket: Any,
    plan: Mapping[str, Any],
    build: Mapping[str, Any],
    facts: Mapping[str, Any],
    ticket_id: Any = None,
) -> dict[str, Any]:
    """The shuttle. Between build and review, someone checks that what came out
    of the last step is actually what the next one needs — and stops here if it
    is not. A review of a half-finished change produces a confident opinion
    about the wrong thing.

    A retry gets exactly the same check as the first try. An incomplete second
    attempt is still incomplete, and "we were already fixing it" is not a reason
    to review a gap.
    """
    # The artifact travels with the question. An earlier version handed the
    # handoff only the summary, the file list and the line counts — and it
    # correctly refused every build for "no patch text was handed off", five
    # epics running. A shuttle that cannot see the cargo cannot judge it.
    patch = str(build.get("patch") or "")
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
                f"Files: {build.get('files_touched')}\nChange facts: {facts}\n"
                f"Commands run (with their real output): {build.get('commands_run')}\n"
                f"Patch ({len(patch)} chars, {'complete' if len(patch) <= PATCH_PREVIEW_CHARS else 'head shown'}):\n"
                f"{patch[:PATCH_PREVIEW_CHARS]}\n\n"
                "List anything missing, and compress the rest into the smallest "
                "brief that lets review start. The patch above IS the artifact under "
                "review: judge whether it and the command evidence are sufficient, not "
                "whether a repository somewhere already contains them."
            ),
        )
    )
    if not handoff.get("complete"):
        missing = ", ".join(handoff.get("missing") or []) or "unspecified"
        raise ContractViolation(
            f"handoff from build to review is incomplete for '{ticket_id if ticket_id is not None else ticket}': {missing}. "
            "The graph stops rather than reviewing a change that is not finished — "
            "a step that builds on a gap is how a phase goes quietly wrong."
        )
    return handoff


def _review_round(
    runner: NodeRunner,
    *,
    context: list[str],
    bound: Mapping[str, Any],
    ticket: Any,
    build: Mapping[str, Any],
    facts: Mapping[str, Any],
    handoff: Mapping[str, Any] | None,
    tier: int,
) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any] | None, str]:
    """One full round of review, and the verdict it reaches.

    Factored out because a retry is reviewed under EXACTLY the same rules as the
    first try — same tier arithmetic, same optional roles, same arbitration
    trigger. A fix loop with a cheaper second pass would be a way of grinding a
    change past its reviewers, which is the thing this loop must not become.
    """
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
        verdict = str(arbitration.get("verdict"))
    elif adversary is not None:
        verdict = "approve" if review.get("verdict") == adversary.get("verdict") == "approve" else "revise"
    else:
        verdict = str(review.get("verdict"))

    return dict(review), adversary, arbitration, verdict


def run(args: Mapping[str, Any], runner: NodeRunner) -> dict[str, Any]:
    """Run the graph. Every input arrives as an argument — no clock, no disk."""
    cartridge = require_cartridge(args)
    run_id, date, ticket = require(args, "run_id", "date", "ticket")
    # The work item's own words travel with its id. Traced plan nodes spent
    # their first turns globbing the repository for a file named like the
    # ticket, because the id was all they were given; a title and a body in
    # the prompt is the difference between a 10-turn plan and a 24-turn one.
    ticket_text = _ticket_text(ticket, args.get("ticket_title"), args.get("ticket_body"))

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
                    f"Scope this work.\n\nTicket: {ticket_text}\nDate: {date}\n\n"
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
        thread=str(ticket),
        schema=PLAN_SCHEMA,
        context=context,
        prompt=(
            f"Decompose this ticket into an ordered plan.\n\nTicket: {ticket_text}\n"
            f"Date: {date}\n\nName the files you expect to touch, and state what is "
            "explicitly out of scope."
        ),
    )

    # Plan, build and the fix-loop retry share one thread: the builder starts
    # from what the planner already read, and a retry from a tree it already
    # edited. Review never joins the thread — a reviewer that inherits the
    # builder's reasoning is the failure the seat exists to prevent.
    build = runner.run(
        role="build",
        tier="standard",
        thread=str(ticket),
        schema=BUILD_SCHEMA,
        context=context,
        prompt=(
            f"Carry out this plan and return the change as a unified diff.\n\n"
            f"Ticket: {ticket_text}\nPlan: {plan}\n\nReturn the patch only — it is applied "
            "by the shell into a worktree, never by you. Include the deterministic "
            "commands you ran and their output."
        ),
    )

    facts = _change_facts(build)
    bound = cartridge.get("skills") or {}
    surfaces = list(args.get("surfaces") or [])
    patterns = list(args.get("patterns") or [])
    tier = review_tier(cartridge, change_facts=facts, surfaces=surfaces, patterns=patterns)

    handoff: dict[str, Any] | None = None
    if "handoff" in bound:
        handoff = _handoff(runner, context=context, ticket=ticket_text, plan=plan, build=build, facts=facts, ticket_id=ticket)

    review, adversary, arbitration, verdict = _review_round(
        runner,
        context=context,
        bound=bound,
        ticket=ticket_text,
        build=build,
        facts=facts,
        handoff=handoff,
        tier=tier,
    )

    # The bounded fix loop. A change sent back goes back to the builder with the
    # critique attached — but the loop is bounded in three separate ways, because
    # an unbounded one is just a machine for grinding a change past its reviewers
    # until someone blinks.
    fix_attempts = args.get("fix_attempts")
    fix_attempts = DEFAULT_FIX_ATTEMPTS if fix_attempts is None else int(fix_attempts)
    attempts = 1
    stopped: str | None = None
    standing: set[str] = set()

    while verdict != "approve" and attempts <= fix_attempts:
        # Every claim raised so far, not merely the last round's. Re-raising an
        # objection from two rounds ago is no more progress than re-raising the
        # one from the last.
        standing |= _claims(adversary)
        critique = _critique(review, adversary, arbitration)

        retry = runner.run(
            role="build",
            tier="standard",
            thread=str(ticket),
            schema=BUILD_SCHEMA,
            context=context,
            prompt=(
                "This change was sent back. Start from the previous patch — apply it "
                "first, then change only what the critique requires — and return a new "
                "unified diff of the whole change.\n\n"
                f"Ticket: {ticket_text}\nPlan: {plan}\n\n"
                f"Previous patch (apply this first; do not redo the work it already did):\n"
                f"{build.get('patch')}\n\n"
                f"Standing critique:\n{critique}\n\n"
                "Every objection above must actually fall — a patch that leaves one "
                "of them standing is not a fix, and saying it is addressed is not the "
                "same as addressing it. Return the patch only — it is applied by the "
                "shell into a worktree, never by you. Include the deterministic "
                "commands you ran and their output."
            ),
        )
        attempts += 1

        # No progress. Comparing the two patches is cheap, deterministic and
        # pure — difflib reads nothing — and it catches the failure mode that
        # matters most: a builder that returns its own diff back, unchanged,
        # and would otherwise buy a second opinion from a fresh reviewer.
        if SequenceMatcher(None, build.get("patch") or "", retry.get("patch") or "").ratio() >= NO_PROGRESS_RATIO:
            # The retry is dropped rather than returned: `build` and `review`
            # must describe the same patch, or the record lies about what was
            # reviewed. The attempt is still counted — it was still spent.
            stopped = "no_progress"
            break

        build = retry
        facts = _change_facts(build)
        if "handoff" in bound:
            handoff = _handoff(runner, context=context, ticket=ticket_text, plan=plan, build=build, facts=facts, ticket_id=ticket)
        tier = review_tier(cartridge, change_facts=facts, surfaces=surfaces, patterns=patterns)
        review, adversary, arbitration, verdict = _review_round(
            runner,
            context=context,
            bound=bound,
            ticket=ticket_text,
            build=build,
            facts=facts,
            handoff=handoff,
            tier=tier,
        )

        # An approval here is not a technicality. The reviewers saw the standing
        # objections in the patch they were given and approved anyway, which is
        # them judging the objections fallen. Their call, not the loop's.
        if verdict == "approve":
            break

        if standing & _claims(adversary):
            stopped = "objection_standing"
            break

    if verdict != "approve" and stopped is None:
        stopped = "attempts_exhausted"

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
                    # Only when there was a loop. A first-try approval says
                    # nothing about a fix loop because there was not one, and a
                    # row reading "attempt 1 of 3" on every clean pass is a row
                    # that stops being read.
                    *(
                        [{"check": "fix loop", "output": f"approved on attempt {attempts} of {fix_attempts + 1}"}]
                        if attempts > 1
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
                # Carried only when it happened, and then always. The ledger
                # cannot refuse to extend a streak on a repeated-attempt pass if
                # the pass never told it there was one.
                attempts=attempts if attempts > 1 else None,
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
        "fix_loop": {"attempts": attempts, "stopped": stopped},
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
        Need("fix_attempts", flag="--fix-attempts", kind="int", required=False,
             help="additional build attempts after the first (default 2); 0 disables the fix loop"),
    ),
)
