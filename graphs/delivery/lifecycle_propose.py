"""lifecycle-propose — the development loop for ONE task.

    scope -> plan -> [plan_alternative -> plan_arbitrate] -> [plan_adversary]
          -> build (worktree) -> handoff -> review -> adversary -> arbitrate -> emit

Takes one task, produces reviewed work and proposals. Nothing is pushed, opened,
or merged. The build node returns a patch; applying it is the shell's job,
inside a worktree the shell owns.

**Solutions compete before anyone builds.** The reviewers after `build` judge
one diff; they can say ship, revise or reject, and nothing else. A different
design only ever appears if the one planner happens to think of it. So the
bracketed nodes let a team hold a competition at the cheap end of the loop: a
second planner writes an independent plan told to differ, an arbiter picks
one or merges them and names the price, and an adversary attacks the winning
plan's claims — files it assumes, steps that cannot be checked, scope that
grew — before a builder spends a budget on it. A plan costs a tenth of a
build, which is why the competition happens here and not between two diffs.
An objection the adversary sustains buys one revision, and the revision goes
back to whoever wrote the plan — the first planner on its thread, the second
on its own, or the arbiter when the plan is a merge of both. A planner is
never handed another planner's plan and told it is its own. Every one of
these roles is optional; unbound means absent. Even bound, the competition and
the attack run only when the tier read off the task's surfaces and patterns
reaches `policy.plan_competition.min_tier` — a docs-only task does not buy a
second planner just because a team happens to have one bound.

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
from typing import Any, NamedTuple

from graphs._contract import (
    ContractViolation,
    epic_shape,
    landing_for,
    proposal,
    require,
    require_cartridge,
    review_tier,
)
from runner.protocol import BudgetStop, NodeRunner

__all__ = ["GRAPH_NAME", "run"]

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
        # Not every refusal costs the same thing, so the shuttle has to say which
        # kind it is. Missing ARTIFACT or missing INPUT — no patch, a file the
        # plan needed that nobody produced — is blocking: there is nothing for a
        # builder to improve and the line stops. Missing EVIDENCE or a thin
        # summary, with the artifact present, is not: the work exists and what
        # it lacks is exactly what one more build attempt can add.
        "blocking": {
            "type": "boolean",
            "description": (
                "true when the artifact itself or an input the plan needed is missing; "
                "false when the artifact is present and only its evidence or summary is incomplete"
            ),
        },
        "missing": {"type": "array", "items": {"type": "string"}},
        "brief": {"type": "string", "description": "the small thing the next step actually needs"},
    },
    "required": ["complete", "blocking", "missing", "brief"],
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

PLAN_CHOICE_SCHEMA = {
    "type": "object",
    "properties": {
        "chosen": {"type": "string", "enum": ["first", "second", "merged"]},
        # Not required: a pick hands the source plan over verbatim, so a plan
        # emitted alongside `first` or `second` is a plan nobody reads, and a
        # deep-tier model should not have to write one to be discarded. A
        # merge with no plan is enforced in _plan_competition, not here: this
        # schema goes to the model's structured-output subset, which has no
        # if/then, and a merge that names no plan is a stopped graph, not a
        # fallback to either planner's.
        "plan": {**PLAN_SCHEMA, "description": "the merged plan; required when chosen is 'merged', omitted on a pick"},
        "reasoning": {"type": "string"},
        "price": {"type": "string", "description": "what choosing this plan costs, in one sentence"},
    },
    "required": ["chosen", "reasoning", "price"],
    "additionalProperties": False,
}

PLAN_ATTACK_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["proceed", "revise"]},
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


DEFAULT_FIX_ATTEMPTS = 2


class _Author(NamedTuple):
    """Who wrote a plan: the role, its tier, and the thread it was written on.

    A revision has to go back to the seat that holds the plan's context. The
    first planner keeps the ticket's thread, which build later joins; the
    second planner and the arbiter each keep a thread of their own, so that a
    revision can continue where they left off without ever joining the first
    planner's — independence is the whole value of a second plan.
    """

    role: str
    tier: str
    thread: str


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

# A budget stop is not a failure to discard; the worktree holds the partial
# patch and the session holds the context. Two continuations, not unbounded —
# past this the task is too large for the slice it was given, and that is a
# scoping problem, not a reason to keep burning budget on the same session.
CONTINUATIONS_MAX = 2


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


def _is_budget_stop(exc: Exception) -> bool:
    """Whether a `RunnerError` is the CLI's dollar-ceiling stop, not some other failure."""
    return "error_max_budget_usd" in str(exc).lower()


def _continue_ok(stop: BudgetStop, *, surfaces: list[str], continuations: int) -> tuple[bool, str]:
    """Whether a budget-stopped build is worth resuming, and why not when it isn't.

    A fact-only check — no model is asked whether to continue, so it costs
    nothing to run on every stop. The three no-go reasons are different
    signals and each names its own remedy: whoever reads the quarantine acts
    on the text, not on a bare "no".
    """
    if not stop.session:
        return False, "no session to resume"

    touched = [
        line[len("+++ b/") :].strip()
        for line in (stop.partial_patch or "").splitlines()
        if line.startswith("+++ b/")
    ]
    if surfaces:
        outside = [path for path in touched if path not in surfaces]
        if outside:
            return False, (
                f"partial work touches {outside[0]} outside the task's surfaces: "
                "re-scope the task"
            )

    if not (stop.partial_patch or "").strip():
        if continuations == 0:
            return True, ""
        return False, (
            "two budget slices produced no change: set budget_usd on the work "
            "item if the task is legitimately this large, otherwise the task "
            "body is the problem"
        )

    if continuations < CONTINUATIONS_MAX:
        return True, ""

    untouched = [surface for surface in surfaces if surface not in touched]
    return False, (
        "continuation cap reached with partial work: split recommended — "
        f"done: {', '.join(touched)}; untouched: {', '.join(untouched)}"
    )


def _resume_build(
    runner: NodeRunner,
    *,
    context: list[str],
    ticket: Any,
    budget_usd: float | None,
    surfaces: list[str],
    stop: BudgetStop,
    continuations: int,
) -> tuple[dict[str, Any] | None, int, str, BudgetStop]:
    """Decide go/no-go on a budget stop and, on go, resume until one finishes
    or the cap refuses another.

    Returns the finished build (`None` on no-go), the updated continuation
    count, the no-go reason (empty on go), and the last `BudgetStop` seen —
    the caller needs it to report what a first-build no-go could not keep.
    """
    while True:
        go, reason = _continue_ok(stop, surfaces=surfaces, continuations=continuations)
        if not go:
            return None, continuations, reason, stop
        try:
            build = runner.run(
                role="build",
                tier="standard",
                thread=str(ticket),
                schema=BUILD_SCHEMA,
                context=context,
                budget_usd=budget_usd,
                prompt=(
                    "Your previous session stopped at its budget ceiling. Nothing "
                    "you did is lost: the worktree holds your partial change and "
                    "this session holds your context. Continue from exactly where "
                    "you stopped — do not start over and do not re-read what you "
                    "already read. Finish the change, run the checks, and return "
                    "the unified diff of the WHOLE change."
                ),
            )
        except BudgetStop as exc:
            continuations += 1
            stop = exc
            continue
        continuations += 1
        return dict(build), continuations, "", stop


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


def _plan_competition(
    runner: NodeRunner,
    *,
    context: list[str],
    ticket: Any,
    date: Any,
    plan: Mapping[str, Any],
    first: _Author,
) -> tuple[dict[str, Any], dict[str, Any], _Author]:
    """A second, independent plan, and a decision between the two.

    The second planner is shown the first plan only so it can avoid repeating
    it — it is told to differ, not to critique — and it never joins the first
    planner's thread, for the same reason review never joins the builder's.
    The arbiter picks, or merges. When it picks, the builder gets the source
    plan VERBATIM, not the arbiter's restatement of it: what was compared is
    what gets built. Only a merge is the arbiter's own plan.

    Returns the winning plan, the record, and the winner's AUTHOR — the seat
    a revision goes back to. A pick's author is the planner that wrote it; a
    merge's author is the arbiter, because neither planner wrote that plan.
    """
    second = _Author("plan_alternative", "standard", f"{first.thread}/plan_alternative")
    arbiter = _Author("plan_arbitrate", "deep", f"{first.thread}/plan_arbitrate")
    alternative = dict(
        runner.run(
            role=second.role,
            tier=second.tier,
            thread=second.thread,
            schema=PLAN_SCHEMA,
            context=context,
            prompt=(
                "A first plan for this ticket already exists. Write a second one "
                "that takes a materially different route — a different "
                "decomposition, different files, or a different order — so that "
                "a comparison is worth making. Do not critique the first plan; "
                "produce a whole plan of your own.\n\n"
                f"Ticket: {ticket}\nDate: {date}\n\n"
                f"First plan (do not repeat it): {plan}\n\n"
                "Name the files you expect to touch, and state what is explicitly "
                "out of scope."
            ),
        )
    )
    choice = dict(
        runner.run(
            role=arbiter.role,
            tier=arbiter.tier,
            thread=arbiter.thread,
            schema=PLAN_CHOICE_SCHEMA,
            context=context,
            prompt=(
                "Two independent plans exist for this ticket. Choose the one the "
                "builder should carry out, or merge them into one that is better "
                "than either. Judge them against the ticket and the repository in "
                "front of you: steps that can be checked, files that exist, scope "
                "that stays bounded. Say what choosing it costs.\n\n"
                f"Ticket: {ticket}\n\nFirst plan: {plan}\n\nSecond plan: {alternative}"
            ),
        )
    )
    chosen = str(choice.get("chosen"))
    merged = choice.get("plan")
    if chosen == "merged" and not merged:
        raise ContractViolation(
            f"plan_arbitrate claimed 'merged' for '{first.thread}' but returned no plan. "
            "A merge is the arbiter's own plan; the graph stops rather than "
            "building the first plan under that name — a revision would go "
            "back to the arbiter carrying a plan it never wrote."
        )
    winner, author = (
        (dict(plan), first) if chosen == "first"
        else (alternative, second) if chosen == "second"
        else (dict(merged), arbiter)
    )
    record = {
        "alternative": alternative,
        "chosen": chosen,
        "reasoning": str(choice.get("reasoning") or ""),
        "price": str(choice.get("price") or ""),
    }
    return winner, record, author


def _plan_tier(cartridge: Mapping[str, Any], *, surfaces: list[str], patterns: list[str]) -> int:
    """The tier a task reads as before anyone has built anything.

    Deliberately not a call into `review_tier`: a plan has no diff yet, so
    change_facts would have to be passed in empty, and `review_tier`'s size
    branches read an empty dict as zero changed lines in zero modules — which
    satisfies `tier0_max_changed_lines` and `tier1_max_changed_lines` for
    every task. A cartridge that sets `tier0_max_changed_lines` (one already
    does, for live epics) would then read every task without a tier2 surface
    as tier 0 before a line of code exists, silently disabling the
    competition under any floor above 0. This reads only the two policy keys
    a pre-build task can actually speak to — the dangerous surfaces and the
    trivial patterns — and never reaches a size branch at all.

    Note for the next reader: this graph never imports `core`, the same way
    it never imports `shell` or `harness` — only the harness imports the
    substrate, which is what lets CI collect this file with agent-cartridges
    absent.
    """
    config = (cartridge.get("policy") or {}).get("review_tier") or {}
    dangerous = set(config.get("tier2_surfaces") or [])
    if dangerous & set(surfaces):
        return 2
    trivial = set(config.get("tier0_patterns") or [])
    if patterns and set(patterns) <= trivial:
        return 0
    return 1


def _plan_gate(
    cartridge: Mapping[str, Any], bound: Mapping[str, Any], surfaces: list[str], patterns: list[str]
) -> tuple[int, int, bool, bool, bool]:
    """Whether the competition and the attack are worth their price on this task.

    Returns the tier, the configured floor, whether the competition PAIR is
    bound, whether the plan attacker is bound, and whether the floor is
    cleared. The two seats are reported separately on purpose: a cartridge that
    binds only the attacker never configured a competition, and must not be
    told one was skipped. The floor comparison is made exactly once, here.
    """
    tier = _plan_tier(cartridge, surfaces=surfaces, patterns=patterns)
    min_tier = int(((cartridge.get("policy") or {}).get("plan_competition") or {}).get("min_tier", 0))
    competition_bound = "plan_alternative" in bound and "plan_arbitrate" in bound
    attack_bound = "plan_adversary" in bound
    return tier, min_tier, competition_bound, attack_bound, tier >= min_tier


def _plan_attack(
    runner: NodeRunner,
    *,
    context: list[str],
    ticket: Any,
    author: _Author,
    plan: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The adversary, moved to the front of the loop.

    After `build`, an objection costs a rebuild. Before it, an objection costs
    one more plan. So the plan's claims — this file exists, this signature is
    what the test will call, this step can be checked without doing the next
    one — get attacked while they are still cheap to be wrong about.

    Bounded to ONE revision, by the plan's AUTHOR on the author's own thread:
    the first planner when its plan won or no competition ran, the second
    planner when the arbiter chose `second`, the arbiter itself when the plan
    is a merge. The objections travel verbatim. Sending a planner another
    planner's plan would hand it a thread already holding its own losing plan
    and call the result a revision. The revision is not attacked again: the
    review round after build is where the built plan gets judged, and a loop
    here would be a second fix loop with none of the first one's accounting.
    """
    attack = dict(
        runner.run(
            role="plan_adversary",
            tier="deep",
            schema=PLAN_ATTACK_SCHEMA,
            context=context,
            prompt=(
                "Your job is to disagree with this plan before anyone builds it. "
                "Attack the claims it rests on: a file or function it assumes "
                "exists, a signature it assumes, a step that cannot be checked, a "
                "step that depends on the next one, scope that has quietly grown. "
                "Check what you can against the repository.\n\n"
                f"Ticket: {ticket}\nPlan: {plan}\n\n"
                "State your strongest objection plainly, even if you conclude the "
                "plan can proceed."
            ),
        )
    )
    if attack.get("verdict") != "revise":
        return dict(plan), {"attack": attack, "revised": False}

    objections = "\n".join(
        f"- {objection.get('claim')} — {objection.get('why_wrong')}"
        for objection in attack.get("objections") or []
        if isinstance(objection, Mapping)
    )
    revised = dict(
        runner.run(
            role=author.role,
            tier=author.tier,
            thread=author.thread,
            schema=PLAN_SCHEMA,
            context=context,
            prompt=(
                "This plan was sent back before build. Revise it so that every "
                "objection below actually falls — a plan that leaves one standing "
                "is not a revision.\n\n"
                f"Ticket: {ticket}\nPlan: {plan}\n\n"
                f"Objections:\n{objections}\n"
                f"Strongest: {attack.get('strongest_objection')}\n\n"
                "Name the files you expect to touch, and state what is explicitly "
                "out of scope."
            ),
        )
    )
    return revised, {"attack": attack, "revised": True}


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

    But an incomplete handoff is not one thing. Stopping the line is right when
    the artifact or an input is missing, because no amount of rebuilding
    conjures an input nobody produced. It is wrong when the patch is there and
    what is missing is evidence about it: that is a gap one more build attempt
    closes, and quarantining it throws away a finished change and buys a whole
    replan to get back to where the run already was. Three of four handoff stops
    in one day were the second kind. So this raises only on the blocking kind,
    and returns the incomplete handoff otherwise for the caller to route into
    the fix loop.

    Whether the artifact is present is decided HERE, from the patch, and only
    then refined by the node's own flag: a shuttle cannot truthfully call an
    artifact missing while holding it, and a graph that took its word for it
    would be re-deciding a deterministic fact with a model call.
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
                "whether a repository somewhere already contains them.\n\n"
                "If it is not complete, say whether the refusal is BLOCKING. Blocking "
                "means the artifact itself or an input the plan needed is absent — "
                "there is no change here, or the work depended on something nobody "
                "produced, and no amount of rebuilding will conjure it. Not blocking "
                "means the change is present and what it lacks is evidence about it: "
                "a check nobody ran, output nobody attached, a claim nobody tested. "
                "That second kind buys one more build attempt; it does not stop the "
                "line, so do not mark it blocking to signal that it matters."
            ),
        )
    )
    # No patch is an absent artifact as a matter of fact, whatever the node
    # said; with a patch in hand, only the node knows whether an INPUT was
    # missing, so its flag decides.
    blocking = not patch.strip() or bool(handoff.get("blocking"))
    if not handoff.get("complete") and blocking:
        missing = ", ".join(handoff.get("missing") or []) or "unspecified"
        raise ContractViolation(
            f"handoff from build to review is incomplete for '{ticket_id if ticket_id is not None else ticket}': {missing}. "
            "The artifact or an input is missing, so the graph stops rather than "
            "reviewing a change that is not finished — a step that builds on a gap "
            "is how a phase goes quietly wrong."
        )
    return handoff


def _handoff_critique(
    handoff: Mapping[str, Any],
) -> tuple[dict[str, Any], None, None, str]:
    """A non-blocking handoff refusal, in the shape the fix loop already carries.

    The loop moves on a review verdict and a critique, so a handoff that refused
    for missing evidence enters it as exactly that: a `revise` whose findings are
    the handoff's own `missing` list, verbatim. No reviewer ran and none is
    invented — the adversary and the arbitration stay `None`, so nothing
    downstream can mistake the shuttle's objection for a second opinion about
    the code.
    """
    missing = [str(item) for item in handoff.get("missing") or [] if str(item).strip()]
    return (
        {
            "verdict": "revise",
            "findings": [
                {"charter_principle": "handoff evidence", "detail": item, "file": ""}
                for item in missing
            ],
            "rationale": str(handoff.get("brief") or "")
            or f"the handoff refused for missing evidence: {', '.join(missing) or 'unspecified'}",
        },
        None,
        None,
        "revise",
    )


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

    # A per-call dollar ceiling for the build role. The harness has no
    # "float" kind (`Need` offers str, int, json_file, jsonl_file,
    # text_or_path — see graphs/_spec.py), so a CLI-supplied value arrives as
    # a raw string; the epic driver hands one straight off parsed YAML as a
    # float already. Coerced once, here, rather than trusted at each site
    # that passes it to the runner.
    raw_build_budget_usd = args.get("build_budget_usd")
    build_budget_usd = None if raw_build_budget_usd is None else float(raw_build_budget_usd)

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

    bound = cartridge.get("skills") or {}
    surfaces = list(args.get("surfaces") or [])
    patterns = list(args.get("patterns") or [])
    gate_tier, gate_min, competition_bound, attack_bound, compete = _plan_gate(cartridge, bound, surfaces, patterns)
    plan_gate = {
        "tier": gate_tier,
        "min_tier": gate_min,
        "competition": competition_bound and compete,
        "attack": attack_bound and compete,
        "ran": (competition_bound or attack_bound) and compete,
    }

    # The first planner keeps the ticket's own thread; build joins it later.
    author = _Author("plan", "standard", str(ticket))
    first_plan = runner.run(
        role=author.role,
        tier=author.tier,
        thread=author.thread,
        schema=PLAN_SCHEMA,
        context=context,
        prompt=(
            f"Decompose this ticket into an ordered plan.\n\nTicket: {ticket_text}\n"
            f"Date: {date}\n\nName the files you expect to touch, and state what is "
            "explicitly out of scope."
        ),
    )

    # The competition needs both halves: an alternative nobody judges is a
    # plan nobody builds, and an arbiter with one plan has nothing to decide.
    competition: dict[str, Any] | None = None
    if "plan_alternative" in bound and "plan_arbitrate" in bound and compete:
        chosen_plan, competition, author = _plan_competition(
            runner, context=context, ticket=ticket_text, date=date, plan=first_plan, first=author
        )
    else:
        chosen_plan = dict(first_plan)

    # A revision goes to whoever wrote the chosen plan, on that seat's thread.
    plan_attack: dict[str, Any] | None = None
    if "plan_adversary" in bound and compete:
        plan, plan_attack = _plan_attack(
            runner, context=context, ticket=ticket_text, author=author, plan=chosen_plan
        )
    else:
        plan = chosen_plan

    # Plan, build and the fix-loop retry share one thread: the builder starts
    # from what the planner already read, and a retry from a tree it already
    # edited. Review never joins the thread — a reviewer that inherits the
    # builder's reasoning is the failure the seat exists to prevent.
    continuations = 0
    continuation_refused: str | None = None
    try:
        build = runner.run(
            role="build",
            tier="standard",
            thread=str(ticket),
            schema=BUILD_SCHEMA,
            context=context,
            budget_usd=build_budget_usd,
            prompt=(
                f"Carry out this plan and return the change as a unified diff.\n\n"
                f"Ticket: {ticket_text}\nPlan: {plan}\n\nReturn the patch only — it is applied "
                "by the shell into a worktree, never by you. No tags, no fences, no trailing "
                "markup of any kind — the text is fed to `git apply` verbatim and a stray "
                "`</patch>` fails the checks. Include the deterministic "
                "commands you ran and their output."
            ),
        )
    except BudgetStop as exc:
        resumed, continuations, reason, stop = _resume_build(
            runner, context=context, ticket=ticket, budget_usd=build_budget_usd,
            surfaces=surfaces, stop=exc, continuations=continuations,
        )
        if resumed is None:
            # There is no reviewed build yet to keep — nothing to idle. The
            # reason travels on the exception itself, since no result is
            # returned for a `continuation_refused` field to live on.
            raise BudgetStop(
                role=stop.role,
                thread=stop.thread,
                session=stop.session,
                spent_usd=stop.spent_usd,
                partial_patch=stop.partial_patch,
                detail=f"{stop.detail} — continuation refused: {reason}",
            ) from stop
        build = resumed

    facts = _change_facts(build)
    tier = review_tier(cartridge, change_facts=facts, surfaces=surfaces, patterns=patterns)

    handoff: dict[str, Any] | None = None
    if "handoff" in bound:
        handoff = _handoff(runner, context=context, ticket=ticket_text, plan=plan, build=build, facts=facts, ticket_id=ticket)

    # A non-blocking refusal costs a build attempt, not the run. Review is
    # skipped — there is nothing yet worth a deep-tier opinion — and the
    # handoff's own list of what is missing is what the builder is sent back
    # with. Paying two reviewers to read a change the shuttle already said is
    # under-evidenced would buy an opinion about the wrong thing.
    if handoff is not None and not handoff.get("complete"):
        review, adversary, arbitration, verdict = _handoff_critique(handoff)
    else:
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

        try:
            retry = runner.run(
                role="build",
                tier="standard",
                thread=str(ticket),
                schema=BUILD_SCHEMA,
                context=context,
                budget_usd=build_budget_usd,
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
                    "shell into a worktree, never by you. No tags, no fences, no trailing "
                    "markup of any kind — the text is fed to `git apply` verbatim and a "
                    "stray `</patch>` fails the checks. Include the deterministic "
                    "commands you ran and their output."
                ),
            )
        except BudgetStop as exc:
            resumed, continuations, reason, _stop = _resume_build(
                runner, context=context, ticket=ticket, budget_usd=build_budget_usd,
                surfaces=surfaces, stop=exc, continuations=continuations,
            )
            if resumed is None:
                # The retry spent the budget without returning a patch, and a
                # continuation was refused. `build` and `review` still
                # describe the last patch actually reviewed — that is the
                # thing worth keeping, not an exception that loses it along
                # with everything the run already earned.
                attempts += 1
                stopped = "budget"
                continuation_refused = reason
                break
            retry = resumed
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
            # Still under-evidenced. The same rule as the first pass: another
            # attempt if the cap allows one, and never a review round bought
            # for a change the shuttle has already refused to hand over.
            if not handoff.get("complete"):
                review, adversary, arbitration, verdict = _handoff_critique(handoff)
                continue
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
                    # Only when a gated seat is bound but the tier never cleared
                    # the floor: a row that always reads "ran" is a row nobody
                    # reads, and a row present whether or not a seat is bound
                    # would claim a skip that was never even offered.
                    # One row per seat that was bound and skipped, named for what
                    # it is: a cartridge with only the attacker bound never had
                    # a competition to skip.
                    *(
                        [{"check": "plan gate", "output": f"tier {gate_tier} vs min {gate_min}: competition skipped"}]
                        if competition_bound and not compete
                        else []
                    ),
                    *(
                        [{"check": "plan gate", "output": f"tier {gate_tier} vs min {gate_min}: plan attack skipped"}]
                        if attack_bound and not compete
                        else []
                    ),
                    # Only when a competition or an attack ran: a row that
                    # always reads "no competition" is a row nobody reads.
                    *(
                        [{"check": "plan competition", "output": f"chose {competition['chosen']}: {competition['reasoning']} (price: {competition['price']})"}]
                        if competition
                        else []
                    ),
                    *(
                        [{"check": "plan adversary", "output": f"{plan_attack['attack'].get('verdict')} — strongest: {plan_attack['attack'].get('strongest_objection')}" + (" — plan revised once" if plan_attack["revised"] else "")}]
                        if plan_attack
                        else []
                    ),
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
                    # Only when the caller overrode the build budget: a row
                    # that always reads the default budget is a row nobody
                    # reads, and present whether or not an override was given
                    # would claim an override that never happened.
                    *(
                        [{"check": "build budget", "output": f"override ${build_budget_usd} per build call"}]
                        if build_budget_usd is not None
                        else []
                    ),
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
        "plan_competition": competition,
        "plan_attack": plan_attack,
        "plan_gate": plan_gate,
        "build": dict(build),
        "review": dict(review),
        "change_facts": facts,
        "fix_loop": {
            "attempts": attempts,
            "stopped": stopped,
            "continuations": continuations,
            **({"continuation_refused": continuation_refused} if continuation_refused is not None else {}),
        },
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
        Need("build_budget_usd", flag="--build-budget-usd", required=False,
             help="a per-call dollar ceiling for the build role, overriding the default "
                  "(a plain number; there is no float kind, so it arrives as a string "
                  "and this graph coerces it)"),
    ),
)
