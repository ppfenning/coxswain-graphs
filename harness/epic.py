"""The epic-swarm driver: a whole initiative, phase by phase, landing nothing.

NOT a graph, and it must never acquire a `SPEC`. It walks a phase graph, blocks
on fan-outs, creates branches, runs checks and merges into a stack — a graph is
`run(args, runner) -> dict`, pure and replayable, and this is none of those. It
lives beside `phase.py` and `invoke.py`, which is where the contract already put
everything that owns a side effect.

What it does, per phase, in this order and no other:

    branch from the parent phase's head -> fan out `lifecycle` per ready task
    -> apply, check and commit each patch in a worktree the harness owns
    -> escalate anything whose diff touched governance
    -> VALIDATE (`phase-validate`, invoked like any other graph)
    -> one gate for the phase -> execute what was cleared -> record the phase

Validation sits between the fan-out and the merges on purpose. The phase verdict
judges the union of what the tasks produced, before any of it is joined to the
phase branch, so a phase that does not add up is caught while nothing has moved.
The alternative — merge first, re-read the branch afterwards — asks the verdict
to be about a state the driver has already committed to.

**Nothing here merges to a default branch.** There is no code path that emits or
executes `merge_main`; the swarm's terminal state is branches and proposals. See
the comment where the merges execute.

Two things are load-bearing about the record. Every phase records its own
manifest under `f"{run_id}:{phase}"` — one cartridge, one scope, so
`_require_single_scope` stays satisfiable — and an auto-cleared proposal gets no
gate diff and no ledger row, because autonomy is spent by acting and re-earned
only at a gate.

Checks are the cartridge's `landing_areas.checks` plus whatever the repository
itself declares in a root `.agent-checks` file, read once at the edge and
merged in by `_Ctx.checks`.
"""

from __future__ import annotations

import contextlib
import difflib
import json
import logging
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal

from core import ledger, workstore
from core.manifest import append_ledger, build_manifest, gate_diff
from core.workstore import WorkStoreError, record_attempt

from graphs._contract import proposal
from graphs.delivery.lifecycle_propose import DEFAULT_FIX_ATTEMPTS
from harness import work_mirror
from harness.autonomy import split_by_policy
from harness.cause_model import cause_evidence, classify_with_model, one_line
from harness.cause_rule import classify_cause
from harness.checks import (
    HARNESS_FAULT_PREFIX,
    _tail_lines,
    all_passed,
    check_feedback,
    checks_evidence,
    collected_ids,
    coverage_floor_holds,
    fixable_checks,
    is_harness_fault,
    quarantine_reason,
    refix_route,
    repo_checks,
    run_checks,
)
from harness.courier_adapter import send as send_to_courier
from harness.digest import build_digest
from harness.escalate import escalate_self_modification, touched_paths
from harness.gate import apply_arm_for, auto_apply, gate
from harness.invoke import Invocation, invoke_graphs
from harness.resume import load_result, reusable, save_result
from harness.store_lease import assert_epoch
from harness.store_write import Store, upsert_work_item
from harness.worktree import apply_patch, create_worktree, keep_worktree, prune_registrations, remove_worktree
from runner.claude_code_runner import files_touched_from_patch
from runner.protocol import LimitStop, RunnerError

__all__ = ["branch_action", "phase_order", "phase_parents", "run_epic"]

_log = logging.getLogger(__name__)

LIFECYCLE = "lifecycle"
VALIDATE = "validate"
# `graphs/ops/triage_quarantine.py`'s `GRAPH_NAME` is "triage" (docs/design/triage.md
# §1), but its `SPEC.name` — the registry key `invoke_graphs` looks up — is
# "triage-quarantine", because "triage" already names alert triage's subcommand.
TRIAGE = "triage-quarantine"

# The principal names the driver AND the graph whose work it records, so a
# ledger row from a swarm stays distinguishable from the same graph run alone.
PRINCIPAL = "epic-swarm(lifecycle-propose)"

# Commits the driver makes are mechanical — it saves an applied patch and joins
# branches, it never authors. The identity is passed with -c so a test, or a
# machine with no global git config, needs no setup to run this.
_IDENTITY = ("-c", "user.email=epic-swarm@invalid", "-c", "user.name=epic-swarm")

_DRAFT_KINDS = frozenset({"draft_pr_create", "self_modification"})

# How much of a task's patch the validators see. All of it for any real task;
# the bound only stops a pathological diff from swamping the phase prompt.
PATCH_FOR_VALIDATION_CHARS = 120_000

# Two recorded attempts and a third run is refused rather than tried again.
# A refusal past this point is not itself an attempt, so it must not grow the
# count it is enforcing — see `_run_phase`, which quarantines these tasks with
# a plain `quarantined.append` rather than `_quarantine_task`.
ATTEMPT_CAP = 2

# Distinct from a clean run's implicit 0, for a caller to act on the pause.
EXIT_PAUSED = 3

# docs/design/landing-model.md §5: the existing style_pass brief, plus the one
# sentence that scopes it to a whole phase rather than one task's diff.
_STYLE_PASS_PROMPT = (
    "Make the narrow style edit an approved diff still needs, or return nothing. "
    "Remove duplication the tickets introduced separately; remove shims whose exit "
    "condition the phase has met."
)

_CHECK_FIX_PROMPT = (
    "The diff below was approved, then a configured check failed on it. Make the smallest "
    "edit that fixes the failure shown, as a unified diff against the tree with the "
    "approved diff already applied, or return nothing."
)

_STYLE_PASS_SCHEMA = {
    "type": "object",
    "properties": {"patch": {"type": "string"}},
    "required": ["patch"],
    "additionalProperties": False,
}


def _carry_forward(body: str, attempts: list[dict[str, Any]], patch: str | None, *, limit: int) -> str:
    """Append a task's own quarantine history to its ticket body, as a reference.

    Pure: no file, no clock. With no attempts the body is untouched — most
    tasks have none. Otherwise the planner and the builder both see why the
    last try was quarantined, and — when a patch was actually saved — what it
    looked like, offered as a reference and never as an approved change.
    """
    if not attempts:
        return body
    header = (
        "",
        "## Previous attempts (recorded by the harness)",
        *(f"- {a.get('run')} ({a.get('phase')}): {a.get('reason')}" for a in attempts),
    )
    reference = (
        (
            "",
            "Reference patch from the last attempt (NOT approved — the reasons above are "
            "why; apply what still fits, address every reason, and re-read the critique "
            "before trusting any line of it):",
            "```diff",
            patch[:limit] if len(patch) <= limit else f"{patch[:limit]}\n... truncated",
            "```",
        )
        if patch and patch.strip()
        else ()
    )
    return "\n".join((body, *header, *reference))


def _git(*args: str, cwd: Path | None = None) -> tuple[bool, str]:
    """Run one git command and report what happened, never what was intended."""
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    return proc.returncode == 0, (proc.stderr or proc.stdout).strip()


def default_branch(origin_head: str | None, local: set[str], head: str) -> str:
    """Origin's default if it is local, else main, else master, else `head`."""
    origin_name = (origin_head or "").removeprefix("origin/")
    if origin_name and origin_name in local:
        return origin_name
    if "main" in local:
        return "main"
    if "master" in local:
        return "master"
    return head


@dataclass(frozen=True)
class _Ctx:
    """Everything the per-phase work needs, fixed for the whole run.

    A frozen record rather than a dozen parameters threaded through six
    functions: the phase loop is where the interesting decisions are, and they
    read better when the configuration is not standing in front of them.
    """

    repo: Path
    cartridge: Mapping[str, Any]
    runner: Any
    specs: Mapping[str, Any]
    run_id: str
    date: str
    max_parallel: int
    ledger_path: Path
    provider_profile: str
    runs_dir: Path
    worktree_root: Path
    assume: str | None
    fix_attempts: int | None
    initiative_id: str
    default_ref: str
    resume_from: str | None = None
    repo_checks: list = field(default_factory=list)
    store: Store | None = None
    epoch: int | None = None
    lease_name: str | None = None
    work_state: str = "files"

    # ── names, in one place, so the topology is readable ─────────────────────
    def phase_branch(self, phase: str) -> str:
        return f"epic/{self.initiative_id}/{phase}"

    def draft_branch(self, phase: str, task: str) -> str:
        # NOT `epic/<initiative>/<phase>/<task>`: git cannot hold both
        # `refs/heads/epic/i/p1` and `refs/heads/epic/i/p1/t1`, because one is a
        # file where the other needs a directory. The draft namespace is
        # therefore flattened with `--` under the phase, which keeps
        # `git branch --list 'epic/<initiative>/*'` reading as the stack it is.
        return f"epic/{self.initiative_id}/{phase}--{task}"

    def scratch_branch(self, task: str) -> str:
        # Harness-owned, per run, never promoted: this is the namespace the
        # contract's worktree exception covers. The draft branch above is
        # created only after the gate.
        return f"agents/{self.run_id}/{task}"

    def phase_worktree(self, phase: str) -> Path:
        return self.worktree_root / self.run_id / phase

    def task_worktree(self, phase: str, task: str) -> Path:
        return self.phase_worktree(phase) / task

    @property
    def checks(self) -> list[Mapping[str, Any]]:
        cartridge_checks = list((self.cartridge.get("landing_areas") or {}).get("checks") or [])
        known = {c.get("cmd") for c in cartridge_checks}
        extra = [c for c in self.repo_checks if c.get("cmd") not in known]
        return cartridge_checks + extra

    @property
    def bound(self) -> Mapping[str, Any]:
        return self.cartridge.get("skills") or {}


# ── the phase graph ─────────────────────────────────────────────────────────


def phase_parents(items: Sequence[Mapping[str, Any]]) -> dict[str, set[str]]:
    """Phase B depends on phase A iff some task in B needs a task in A.

    Derived from the task edges rather than declared, because the task DAG is
    the thing `initiative-decompose`'s adversary actually attacked. A separately
    declared phase order would be a second source of truth that nobody checked.
    """
    phase_of = {str(item["id"]): str(item.get("phase") or "") for item in items}
    state_of = {str(item["id"]): item.get("state") for item in items}
    parents: dict[str, set[str]] = {phase: set() for phase in phase_of.values() if phase}
    for item in items:
        here = str(item.get("phase") or "")
        for need in item.get("needs") or []:
            there = phase_of.get(str(need))
            # A `done` need is already on the base ref, so it imposes no stacking
            # requirement — only an unsatisfied need makes its phase a parent.
            if here and there and there != here and state_of.get(str(need)) != "done":
                parents[here].add(there)
    return parents


def phase_order(parents: Mapping[str, set[str]]) -> tuple[list[str], list[str]]:
    """Phases in dependency order with ties broken by name, then the unorderable.

    Ties break by name so two runs over one initiative walk the phases in the
    same order — the same reason `invoke_graphs` reads its results back sorted.
    Whatever is left over sits in a cycle BETWEEN phases: a task DAG can be
    acyclic while the phase graph it induces is not, and a cycle is reported
    rather than resolved.
    """
    remaining = {phase: set(deps) for phase, deps in parents.items()}
    ordered: list[str] = []
    while remaining:
        free = sorted(phase for phase, deps in remaining.items() if not deps - set(ordered))
        if not free:
            break
        for phase in free:
            ordered.append(phase)
            del remaining[phase]
    return ordered, sorted(remaining)


_EVIDENCE_LINE_CAP = 400


def _read_evidence_file(worktree: str, rel: str) -> str | None:
    """The reader `phase-validate` calls for a chunk verdict's `needs_evidence`.

    The one place this driver opens a file on the graph's behalf, injected in
    as an argument per docs/GRAPH-CONTRACT.md clause 4 — the graph itself
    never touches a filesystem. The file-count cap and the check that keeps a
    request inside `worktree` are the graph's own, over the names alone; this
    only caps line count and reports an unreadable path as `None`, never a
    raise, because one bad name must not cost the rest of the phase's verdict.
    """
    try:
        text = Path(worktree, rel).read_text()
    except OSError:
        return None
    return "\n".join(text.splitlines()[:_EVIDENCE_LINE_CAP])


def _phase_goal(initiative: Mapping[str, Any], phase: str) -> str:
    """The phase's ORIGINAL goal — the thing `validate_phase` judges against.

    A work store's phases are bare names (`workstore.phases` returns the set of
    directory names), so where a goal was never recorded the honest substitute
    is the phase's name plus the initiative's own prose. Not a restatement of
    the task list: that is exactly what the phase verdict must never be allowed
    to reduce to. Where a decompose-produced initiative carries `{id, goal}`
    phase entries, they win.
    """
    for entry in initiative.get("phases") or []:
        if isinstance(entry, Mapping) and str(entry.get("id")) == phase and entry.get("goal"):
            return str(entry["goal"])
    body = str(initiative.get("body") or "").strip()
    return f"phase '{phase}' of initiative '{initiative.get('id')}'" + (f"\n\n{body}" if body else "")


# ── branch topology ─────────────────────────────────────────────────────────


def _branch_exists(ctx: _Ctx, branch: str) -> bool:
    ok, _ = _git("-C", str(ctx.repo), "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
    return ok


def _open_phase_worktree(ctx: _Ctx, phase: str, base_ref: str) -> tuple[bool, str, bool]:
    """Get a worktree on the phase branch. Returns (ok, detail, reused).

    Re-entrancy is the point: a second driver run over the same initiative
    builds on the branch the first one left, rather than starting a parallel
    one beside it under a different run id.

    Prunes stale `git worktree` registrations first: a crashed prior run can
    leave a phantom entry pointing at a directory that is already gone, and
    that phantom is what blocked `tools-chair-rename-18` on 2026-09-08.
    """
    prune_registrations(ctx.repo)
    branch = ctx.phase_branch(phase)
    worktree = ctx.phase_worktree(phase)
    worktree.parent.mkdir(parents=True, exist_ok=True)
    if _branch_exists(ctx, branch):
        ok, detail = _git("-C", str(ctx.repo), "worktree", "add", str(worktree), branch)
        return ok, detail, True
    ok, detail = create_worktree(ctx.repo, worktree, branch=branch, base=base_ref)
    return ok, detail, False


def _parent_head_moved(ctx: _Ctx, phase: str, base_ref: str) -> bool:
    """Is the phase branch still stacked on the parent's CURRENT head?

    `merge-base --is-ancestor` is the whole question: if the parent's head is no
    longer an ancestor of this branch, the ground under the stack moved, and
    everything above it is building on a base that is no longer there.
    """
    ok, _ = _git("-C", str(ctx.repo), "merge-base", "--is-ancestor", base_ref, ctx.phase_branch(phase))
    return not ok


def branch_action(reused: bool, head_moved: bool, has_own_commits: bool) -> str:
    """Block only a stale branch that has something of its own to lose; recreate the rest."""
    if not (reused and head_moved):
        return "proceed"
    return "block" if has_own_commits else "recreate"


def _task_of(subject: str) -> str | None:
    head, sep, task = subject.partition(": ")
    return task if sep and head.startswith("epic ") else None


def unlanded(cherry_lines: Sequence[str], done_tasks: set[str]) -> list[str]:
    """Subjects of `git cherry -v` `+` lines that are neither a done task's commit nor one of our merges.

    A landing can rewrite a done task's patch, so its patch-id no longer matches main's.
    A task literally named `merge ...` reads as a merge commit.
    """
    subjects = [line.split(" ", 2)[2] if line.count(" ") >= 2 else "" for line in cherry_lines if line.startswith("+ ")]
    return [s for s in subjects if (t := _task_of(s)) is None or not (t in done_tasks or t.startswith("merge "))]


def _phase_branch_has_unlanded_commits(ctx: _Ctx, phase: str, done_tasks: set[str]) -> bool:
    """Patch-id semantics, less commits of tasks the work store already calls done."""
    ok, out = _git("-C", str(ctx.repo), "cherry", "-v", ctx.default_ref, ctx.phase_branch(phase))
    return ok and bool(unlanded(out.splitlines(), done_tasks))


def _rebase(ctx: _Ctx, phase: str, base_ref: str) -> tuple[bool, str]:
    """Replay the phase branch onto the parent's new head, or leave it untouched.

    A conflict aborts and quarantines the phase with git's own diagnosis. There
    is no path here that resolves one unattended: `stack_rebase` is a write kind
    precisely because rewriting a branch other work is stacked on can silently
    discard a commit, and a driver guessing at a resolution would be doing
    exactly that.
    """
    worktree = ctx.phase_worktree(phase)
    branch = ctx.phase_branch(phase)
    ok, fork = _git("-C", str(worktree), "merge-base", base_ref, branch)
    if not ok:
        return False, f"no merge base between {base_ref} and {branch}: {fork}"
    ok, detail = _git(*_IDENTITY, "-C", str(worktree), "rebase", "--onto", base_ref, fork.strip())
    if not ok:
        _git("-C", str(worktree), "rebase", "--abort")
        return False, detail
    return True, f"rebased {branch} onto {base_ref}"


# ── one task's build, applied and measured in a worktree the harness owns ────


def _trace_evidence(calls: Sequence[Any], task: str, patch: str) -> list[dict[str, Any]]:
    """Evidence rows the harness itself observed, per docs/design/validator-reach.md §1.

    Commands are selected by `task_id` — stamped onto the call ledger by
    `ClaudeCodeRunner.run`'s own `task` kwarg — never by `files_touched`, which two
    tasks (or a fix-loop retry) can share. `ctx.runner.calls` accumulates across every
    task and retry, so the LAST matching `role: "build"` call is the one whose patch
    this record actually applied. One `command` row per `commands_run` entry with
    `source == "trace"`, in trace order — a `self_report` entry is a claim, not an
    observation, and is never folded in.

    A resumed task (`--resume-from`) makes no build call in THIS run, so no ledger
    entry matches at all — `files_touched` then falls back to the reconciled `patch`
    itself, still exactly one row, never empty for a non-empty patch.
    """
    call = next(
        (c for c in reversed(calls) if isinstance(c, Mapping) and c.get("role") == "build" and c.get("task_id") == task),
        {},
    )
    commands = [
        {
            "check": "command",
            "source": "trace",
            "output": f"{entry.get('command', '')}\n{_tail_lines(str(entry.get('output') or ''))}",
        }
        for entry in (call.get("commands_run") or [])
        if isinstance(entry, Mapping) and entry.get("source") == "trace"
    ]
    files = call.get("files_touched") or files_touched_from_patch(patch)
    return [*commands, {"check": "files_touched", "source": "trace", "output": "\n".join(files)}]


def _build_task(
    ctx: _Ctx, *, phase: str, task: str, result: Mapping[str, Any], verify: Sequence[str] = ()
) -> dict[str, Any]:
    """Apply one task's patch on a scratch branch off the phase branch, and check it.

    Returns the record whose evidence rows the gate will read. Whether the tests
    pass is not something a review node gets to assert, and a task whose checks
    fail has not met its done criteria — done criteria consume machine evidence,
    not claims — so a failure here quarantines WITH the evidence attached rather
    than proposing a merge and hoping.
    """
    patch = str((result.get("build") or {}).get("patch") or "")
    worktree = ctx.task_worktree(phase, task)
    branch = ctx.scratch_branch(task)
    record: dict[str, Any] = {"id": task, "phase": phase, "branch": branch, "evidence": []}

    ok, detail = create_worktree(ctx.repo, worktree, branch=branch, base=ctx.phase_branch(phase))
    if not ok:
        record["quarantine"] = f"worktree {branch} could not be created: {detail}"
        return record

    ok, detail = apply_patch(patch, worktree)
    record["evidence"].append(
        {"check": "patch_apply", "output": f"ok — applied in {worktree}" if ok else f"FAIL — {detail}"}
    )
    if not ok:
        record["quarantine"] = f"patch did not apply: {detail}"
        return record
    record["evidence"].extend(_trace_evidence(getattr(ctx.runner, "calls", None) or [], task, patch))

    # Commit BEFORE the checks run, so the branch holds exactly the applied
    # patch and nothing else. Checks execute things — a test run drops
    # __pycache__ and friends into the worktree, and an add -A afterwards would
    # commit those byproducts, which then differ per task and collide as binary
    # conflicts at merge time. Found by running the whole driver against a real
    # repository, which is the only place a bug like this can live.
    ok, detail = _git(*_IDENTITY, "-C", str(worktree), "add", "-A")
    if ok:
        ok, detail = _git(
            *_IDENTITY, "-C", str(worktree), "commit", "--allow-empty", "-q",
            "-m", f"epic {ctx.run_id}: {task}",
        )
    if not ok:
        record["quarantine"] = f"the applied patch could not be committed: {detail}"
        return record

    # Mechanical, model-free: a lint fix is not a build attempt, so it runs
    # here rather than looping the fix back through review. Its changes are
    # amended into the task's own commit, so the branch stays one commit (the
    # land takes exactly one), and get folded into `result`'s own patch so
    # every later reader — validation, escalation, merge — sees the fixed
    # file, not the one the build actually produced.
    fixable = fixable_checks(ctx.checks)
    record["lint_fix_checks"] = [c["name"] for c in fixable]
    record["lint_fixed"] = False
    if fixable:
        run_checks(worktree, fixable)
        _, dirty = _git("-C", str(worktree), "status", "--porcelain")
        if dirty:
            _git(*_IDENTITY, "-C", str(worktree), "add", "-A")
            ok, detail = _git(*_IDENTITY, "-C", str(worktree), "commit", "-q", "--amend", "--no-edit")
            if ok:
                record["lint_fixed"] = True
                build_field = result.get("build")
                _, folded = _git("-C", str(worktree), "diff", ctx.phase_branch(phase), "HEAD")
                if isinstance(build_field, dict) and folded:
                    build_field["patch"] = folded + "\n"

    if ctx.checks:
        results = run_checks(worktree, ctx.checks)
        record["checks"] = results
        record["evidence"].extend(checks_evidence(results))
        reason = quarantine_reason(results)
        if reason:
            record["quarantine"] = reason

    # The ticket's own verification commands, run here because the builder's
    # sandbox cannot. Their output is evidence for the reviewers; a failing one
    # never quarantines, so its results stay out of `quarantine_reason`.
    if verify:
        ran = run_checks(worktree, [{"name": str(i), "cmd": cmd} for i, cmd in enumerate(verify, 1)])
        record["verify"] = ran
        record["evidence"].extend(checks_evidence(ran, prefix="verify", always_tail=True))
    return record


def _verify_of(by_id: Mapping[str, Mapping[str, Any]], task: str) -> list[str]:
    return [str(c) for c in (by_id.get(task) or {}).get("verify") or []]


def _lifecycle_invocation(
    ctx: _Ctx, task: Mapping[str, Any], *, body: str, fix_attempts: int | None
) -> Invocation:
    return Invocation(
        id=str(task["id"]),
        graph=LIFECYCLE,
        args={
            "date": ctx.date,
            "ticket": task["id"],
            "work_item": True,
            "ticket_title": task.get("title") or "",
            "ticket_body": body,
            "cartridge": ctx.cartridge,
            "surfaces": list(task.get("surfaces") or []),
            "patterns": list(task.get("patterns") or []),
            "tier": dict(task.get("tier") or {}),
            **({"fix_attempts": fix_attempts} if fix_attempts is not None else {}),
            **({"build_budget_usd": task["budget_usd"]} if task.get("budget_usd") is not None else {}),
        },
    )


def _style_fix(ctx: _Ctx, *, phase: str, task: str, result: Mapping[str, Any], build: dict[str, Any]) -> bool:
    """One `style_pass` over an approved build whose lint failed, then the checks again.

    The seat is a model and no reviewer sees its edit, so it is bounded three
    ways. Its patch passes the coverage floor `_trim_phase` uses, no collected
    test id may disappear. Only the paths it names are staged, because the
    checks have already written byproducts into this worktree. And the edit is
    recorded as an evidence row, for whoever reads the gate. True when the patch
    applied, held the floor and was committed; `build` then carries the fresh
    check results and `result`'s patch the folded diff. False restores the
    worktree, for a fall back to a revise.
    """
    worktree = ctx.task_worktree(phase, task)
    before = _collected_ids(worktree)
    if before is None:
        return False  # no floor can be measured, so no model call is worth buying
    patch = str((result.get("build") or {}).get("patch") or "")
    edit = ctx.runner.run(
        role="style_pass",
        tier="standard",
        schema=_STYLE_PASS_SCHEMA,
        task=task,
        context=list(ctx.cartridge.get("context") or []),
        prompt=(
            f"{_CHECK_FIX_PROMPT}\n\nTicket: {task}\n\nFailing checks:\n{check_feedback(build['checks'])}"
            f"\n\nApproved diff:\n{patch[:PATCH_FOR_VALIDATION_CHARS]}"
        ),
    ).get("patch")
    if not str(edit or "").strip():
        return False
    if not apply_patch(str(edit), worktree)[0]:
        return False
    after = _collected_ids(worktree)
    if after is None or not coverage_floor_holds(before, after):
        _git("-C", str(worktree), "reset", "--hard", "-q")
        _git("-C", str(worktree), "clean", "-fdq")
        build["evidence"].append({"check": "style_pass", "output": "refused (coverage floor)"})
        return False
    paths = touched_paths(str(edit))
    staged, _ = _git(*_IDENTITY, "-C", str(worktree), "add", "-A", "--", *paths)
    if not staged or not _git(*_IDENTITY, "-C", str(worktree), "commit", "-q", "-m", "style pass")[0]:
        return False
    _, folded = _git("-C", str(worktree), "diff", ctx.phase_branch(phase), "HEAD")
    build_field = result.get("build")
    if isinstance(build_field, dict) and folded:
        build_field["patch"] = folded + "\n"
    results = run_checks(worktree, ctx.checks)
    build["checks"] = results
    # Only `checks:` rows are replaced; `verify:` rows stay as run before the style edit.
    build["evidence"] = [
        *(row for row in build["evidence"] if not str(row.get("check")).startswith("checks:")),
        {"check": "style_pass", "output": f"edit applied after approval, not reviewed: {', '.join(paths)}"},
        *checks_evidence(results),
    ]
    build["quarantine"] = quarantine_reason(results)
    return True


def _refix(
    ctx: _Ctx,
    by_id: Mapping[str, Mapping[str, Any]],
    *,
    phase: str,
    task: str,
    result: dict[str, Any],
    build: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Send an approved build whose checks failed back through the fix loop, while attempts remain.

    The loop approved this build, and a configured check then failed on it: a
    one-token lint error cost a whole rerun when that was quarantined at once.
    The check's own output goes back to the builder, either through the
    `style_pass` seat for a lint-only failure or as a fresh lifecycle run whose
    ticket body carries it, and the checks run again on what comes back.
    Attempts are the loop's own budget, `fix_attempts` plus the first build,
    less what the loop already spent and what each re-entry spends. Once none
    remain, or a re-entry is not approved, the build quarantines as before.
    A task gets at most one `style_pass` edit; a revise is a whole reviewed
    lifecycle run, and each one leaves an evidence row saying what it spent.
    """
    limit = DEFAULT_FIX_ATTEMPTS if ctx.fix_attempts is None else ctx.fix_attempts
    spent = float((result.get("fix_loop") or {}).get("attempts") or 1)
    item = {**(by_id.get(task) or {}), "id": task}
    routes: list[str] = []
    while build.get("quarantine") and "checks" in build:
        left = limit + 1 - spent
        # One unreviewed style edit per task: a second failure goes to a reviewed revise.
        style_open = "style_pass" in ctx.bound and "style_pass" not in routes
        route = refix_route(build["checks"], ctx.checks, attempts_left=left, style_bound=style_open)
        if route == "quarantine":
            break
        if route == "style_pass" and _style_fix(ctx, phase=phase, task=task, result=result, build=build):
            spent += 1
            routes.append("style_pass")
            continue
        failed = {
            "run": ctx.run_id,
            "phase": phase,
            "reason": f"approved, then failed its configured checks:\n{check_feedback(build['checks'])}",
        }
        body = _carry_forward(
            item.get("body") or "",
            [*(item.get("attempts") or []), failed],
            str((result.get("build") or {}).get("patch") or ""),
            limit=PATCH_FOR_VALIDATION_CHARS,
        )
        retried, _, failures = invoke_graphs(
            [_lifecycle_invocation(ctx, item, body=body, fix_attempts=max(int(left) - 1, 0))],
            specs=ctx.specs,
            runner=ctx.runner,
            run_id=f"{ctx.run_id}:{phase}:refix{len(routes) + 1}",
            max_parallel=1,
        )
        why = failures[0] if failures else None
        if why is None and retried[0].get("failed_node") is not None:
            why = f"node '{retried[0]['failed_node']}' failed"
        if why is None:
            why = _unapproved(retried[0])
        if why is not None:
            build["evidence"].append({"check": "fix loop re-entry", "output": f"not approved: {why}"})
            break
        result = retried[0]
        result.setdefault("initiative", ctx.initiative_id)
        result.setdefault("phase", phase)
        _save_result(ctx, result, phase=phase, task=task)
        spent += float((result.get("fix_loop") or {}).get("attempts") or 1)
        routes.append("revise")
        names = ", ".join(str(c["name"]) for c in build["checks"] if not c.get("passed"))
        remove_worktree(ctx.repo, ctx.task_worktree(phase, task))
        _git("-C", str(ctx.repo), "branch", "-D", ctx.scratch_branch(task))
        build = _build_task(ctx, phase=phase, task=task, result=result, verify=_verify_of(by_id, task))
        build["evidence"].append(
            {"check": "fix loop re-entry", "output": f"revise after {names}: attempts spent {spent:g} of {limit + 1}"}
        )
    return result, ({**build, "refix": routes} if routes else build)


def _unapproved(result: Mapping[str, Any]) -> str | None:
    """Why the fix loop refused this task, or None when it approved it.

    Read from the loop's own record, not re-derived. `lifecycle-propose` emits a
    `draft_pr_create` proposal when and only when its reviewers approved the
    build; a result carrying none is a change the loop already decided against,
    and `fix_loop.stopped` says on what grounds.

    The epic used to apply that patch anyway: it opened a worktree, ran the
    checks, and then paid a chunk validator and a phase validator to look at a
    task whose `review_verdict` still read `revise`. The validator refused it —
    correctly, on evidence that said the reviewers wanted changes — but it
    refused with its own reasoning rather than the loop's, so the record named
    a validator gap where the real answer was "the fix build changed nothing".
    A verdict a later build was supposed to supersede, and did not, must never
    reach a validator as though it were current.
    """
    if any(
        isinstance(item, Mapping) and item.get("kind") == "draft_pr_create"
        for item in result.get("proposals") or []
    ):
        return None
    loop = result.get("fix_loop") or {}
    stopped = str(loop.get("stopped") or "") or "the reviewers did not approve the build"
    attempts = loop.get("attempts")
    counted = f" after {attempts} build attempt{'s' if attempts != 1 else ''}" if attempts else ""
    arbitration = result.get("arbitration")
    arbitration = arbitration if isinstance(arbitration, Mapping) else {}
    if arbitration.get("verdict"):
        verdict = str(arbitration["verdict"])
        reasoning = str(arbitration.get("reasoning") or "").split(". ", 1)[0].split(".", 1)[0]
        detail = f", sided_with {arbitration.get('sided_with')}: {reasoning}"
    else:
        verdict = str((result.get("review") or {}).get("verdict") or "") or "none recorded"
        objections = (result.get("adversary") or {}).get("objections") or []
        detail = f": {objections[0].get('claim') or ''}" if objections else ""
    return (
        f"the fix loop stopped: {stopped}{counted}; the last review verdict was "
        f"'{verdict}'{detail} and no build was approved"
    )


def task_outcome(
    review_verdict: str | None,
    arbitration_verdict: str | None,
    quarantine_reason: str | None,
    landed: bool,
) -> str:
    """One of `landed`, `approved_not_landed`, `rejected`, `harness_fault`."""
    if landed:
        return "landed"
    if quarantine_reason is not None and is_harness_fault(quarantine_reason):
        return "harness_fault"
    if review_verdict == "approve" or arbitration_verdict == "approve":
        return "approved_not_landed"
    return "rejected"


# ── the driver ──────────────────────────────────────────────────────────────

WORK_STATES = ("files", "store")


def checked_work_state(value: Any) -> str:
    """`value` when it is one of WORK_STATES; anything else is refused, never read as the default."""
    if value not in WORK_STATES:
        raise ValueError(f"provider profile 'work_state' must be one of {', '.join(WORK_STATES)}, not {value!r}")
    return str(value)


def work_state_of(profile: Mapping[str, Any]) -> str:
    """The profile's `work_state`: which side owns a task's state. An absent key means files."""
    return checked_work_state(profile.get("work_state", "files"))


def run_epic(
    *,
    initiative: Mapping[str, Any],
    repo: Path | str,
    cartridge: Mapping[str, Any],
    runner: Any,
    specs: Mapping[str, Any],
    run_id: str,
    date: str,
    max_parallel: int,
    ledger_path: Path | str,
    provider_profile: str,
    runs_dir: Path | str,
    worktree_root: Path | str,
    assume: str | None = None,
    fix_attempts: int | None = None,
    resume_from: str | None = None,
    keep_worktrees: bool = False,
    store: Store | None = None,
    epoch: int | None = None,
    lease_name: str | None = None,
    work_state: str = "files",
) -> dict[str, Any]:
    """Drive a whole initiative: every phase, in dependency order, landing nothing.

    `initiative` is `core.workstore.read_initiative(...)` output — read by the
    CLI and passed in, because a driver that read the store itself would put the
    filesystem back inside the thing under test. `repo` is required: stacking is
    real branches in a real repository, and there is no honest way to fake that.

    Failure is continued-and-quarantined at BOTH grains. A task that fails its
    checks is set aside and its siblings still gate; a phase that does not meet
    its goal blocks its own dependents and nothing else. One task must not take
    a phase with it, and one phase must not take an initiative with it.

    Every exit — normal completion, an early return, or an exception raised
    anywhere in the phase loop — cleans up this run's worktrees in a
    `finally`: removed by default, or moved under `_kept/<run_id>` when
    `keep_worktrees` is set. The primitives live in `harness.worktree`; this
    only decides which one to call.

    The `store` is required: the run is recorded there as phases, tasks, attempts, gate
    decisions and ledger rows, and no per-phase manifest file is written. With an
    `epoch`, every leader-only write first asserts that epoch against the lease
    `lease_name` and is refused when it is stale.
    """
    if store is None:
        raise ValueError(
            "the epic driver needs a store: phases, tasks, attempts, gate decisions and ledger rows are recorded there"
        )
    work_state = checked_work_state(work_state)
    repo = Path(repo)
    ctx: _Ctx | None = None
    try:
        head_ok, head_out = _git("-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD")
        origin_ok, origin_out = _git("-C", str(repo), "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
        local_ok, local_out = _git("-C", str(repo), "for-each-ref", "--format=%(refname:short)", "refs/heads")
        try:
            # `utf-8-sig` also strips a leading BOM, which would otherwise survive
            # into the first command's name and cmd and never resolve as a shell
            # command. Either way a malformed file degrades to no repo checks,
            # never to a run that dies on somebody else's typo.
            agent_checks_text = (repo / ".agent-checks").read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            agent_checks_text = ""
        ctx = _Ctx(
            repo=repo,
            cartridge=cartridge,
            runner=runner,
            specs=specs,
            run_id=run_id,
            date=date,
            max_parallel=max_parallel,
            ledger_path=Path(ledger_path),
            provider_profile=provider_profile,
            runs_dir=Path(runs_dir),
            worktree_root=Path(str(worktree_root)).expanduser(),
            assume=assume,
            fix_attempts=fix_attempts,
            resume_from=resume_from,
            repo_checks=repo_checks(agent_checks_text),
            store=store,
            epoch=epoch,
            lease_name=lease_name,
            work_state=work_state,
            initiative_id=str(initiative.get("id")),
            # An unparented phase branches from the repository's default branch, read
            # once here so every phase in a run stacks on the same ground, whatever
            # branch the checkout has out.
            default_ref=default_branch(
                origin_out.strip() if origin_ok else None,
                set(local_out.split()) if local_ok else set(),
                head_out.strip() if head_ok else "HEAD",
            ),
        )

        # The driver's own view of the work. `ready_tasks` answers from item state,
        # so an EXECUTED `state_move` has to be reflected here or the next phase's
        # tasks never become ready within this run. The arm remains the single
        # writer of the store on disk; this is the driver keeping its own copy
        # honest about what the arm just did.
        items = [dict(item) for item in initiative.get("items") or []]
        _mirror_read(ctx, items)

        parents = phase_parents(items)
        ordered, cyclic = phase_order(parents)

        phases: list[dict[str, Any]] = []
        tasks: list[dict[str, Any]] = []
        quarantined: list[dict[str, Any]] = []
        proposals: list[dict[str, Any]] = []
        complete: set[str] = set()
        stacks_rebased = 0
        paused_until: str | None = None

        def add_phase(record: dict[str, Any]) -> None:
            phases.append(record)
            _record_phase(ctx, record)

        for phase in cyclic:
            add_phase(
                {
                    "phase": phase,
                    "status": "blocked",
                    "reason": (
                        "phase dependency cycle: the task DAG is acyclic but the phase graph it "
                        "induces is not, so no order over these phases exists"
                    ),
                }
            )

        for phase in ordered:
            parent_phases = sorted(parents.get(phase) or ())

            if len(parent_phases) > 1:
                # Two parents is a merge of two stacks, and a v1 stack has one base
                # ref. Refusing beats picking one parent and silently building on
                # half the ground.
                add_phase(
                    {
                        "phase": phase,
                        "status": "blocked",
                        "parents": parent_phases,
                        "reason": (
                            f"multiple parent phases ({', '.join(parent_phases)}); "
                            "v1 stacks support one parent"
                        ),
                    }
                )
                continue

            parent = parent_phases[0] if parent_phases else None
            if parent is not None and parent not in complete:
                # v1 is blanket no: a phase unblocks its dependents only when
                # `validate_phase` says the goal is met. The validator reports
                # `quarantine_blocks_dependents`; nothing acts on it yet.
                add_phase(
                    {
                        "phase": phase,
                        "status": "blocked",
                        "parents": parent_phases,
                        "reason": f"parent phase '{parent}' did not meet its goal; dependents do not run",
                    }
                )
                continue

            try:
                record = _run_phase(ctx, initiative=initiative, phase=phase, parent=parent, items=items)
            except LimitStop as exc:
                # No quarantine, no attempt, every sibling task left as-is: stop now.
                paused_until = exc.detail.split("resets", 1)[-1].strip()
                break
            # Threads live for a phase: every task's plan/build/retry is done by now.
            close = getattr(ctx.runner, "close", None)
            if callable(close):
                close()
            tasks.extend(record.pop("task_records"))
            quarantined.extend(record.pop("quarantined"))
            proposals.extend(record.pop("batch"))
            stacks_rebased += 1 if record.get("rebased") else 0
            add_phase(record)
            if record["status"] == "complete":
                complete.add(phase)

        approved_not_landed = [t for t in tasks if t.get("outcome") == "approved_not_landed"]
        # `outcome == "landed"` only ever meant "merged into the phase stack,
        # not quarantined" — never a claim that `cox runs land` ran — so any
        # such task whose item still reads `approved` is unlanded too.
        merged_not_landed = [t for t in tasks if t.get("outcome") == "landed" and t.get("state") == "approved"]
        # Same command as the printed line, kept alongside the task so the
        # courier note below can never drift from what a human already reads.
        # `land` subsumes `recover` for this purpose and never needs a phase
        # branch resolved, so it is the one printed here; `recover` stays
        # available on the command line for a human repairing the stack itself.
        unlanded = [
            (t, f"cox runs land {run_id} --repo {repo} --task {t['id']} --apply")
            for t in approved_not_landed + merged_not_landed
        ]
        for t, command in unlanded:
            send_to_courier(f"coxswain://task/{t['id']}", "chair", command)
        return {
            "run_id": run_id,
            "date": date,
            "initiative": ctx.initiative_id,
            "phases": phases,
            "tasks": tasks,
            "quarantined": quarantined,
            "proposals": proposals,
            **({"paused_until": paused_until, "exit_code": EXIT_PAUSED} if paused_until else {}),
            "exit_summary": (
                [f"paused: account session limit, resets {paused_until}"] if paused_until else []
            ) + [
                f"approved but not landed: {t['id']} — {command}" for t, command in unlanded
            ],
            "totals": {
                "phases_complete": sum(1 for p in phases if p["status"] == "complete"),
                "phases_partial": sum(1 for p in phases if p["status"] == "partial"),
                "phases_blocked": sum(1 for p in phases if p["status"] == "blocked"),
                "tasks_quarantined": sum(1 for q in quarantined if q.get("grain") == "task"),
                "approved_not_landed": len(approved_not_landed) + len(merged_not_landed),
                "stacks_rebased": stacks_rebased,
            },
        }
    finally:
        # Every exit — the return above, a raise from anywhere in this try, or
        # a signal delivered as KeyboardInterrupt — lands here. Nothing before
        # `ctx` exists can have made a worktree, so there is nothing to do yet.
        if ctx is not None:
            run_dir = ctx.worktree_root / ctx.run_id
            if keep_worktrees:
                # `keep_worktree` moves ONE named worktree to
                # `_kept/<run_id>/<name>`; run_dir's own name is the run id,
                # so calling it on run_dir directly would double that segment
                # into `_kept/<run_id>/<run_id>`. Called once per phase
                # directory instead, each move lands where work-shape.md §6
                # says a kept run lands: `_kept/<run_id>/<phase>`.
                if run_dir.is_dir():
                    for phase_dir in sorted(run_dir.iterdir()):
                        keep_worktree(ctx.repo, phase_dir, ctx.worktree_root, ctx.run_id)
                    if not any(run_dir.iterdir()):
                        run_dir.rmdir()
            else:
                remove_worktree(ctx.repo, run_dir)


def _quarantine_task(
    ctx: _Ctx,
    by_id: Mapping[str, dict[str, Any]],
    *,
    phase: str,
    task: str,
    reason: str,
    kind: Literal["refused", "no_work", "unverified", "infra"],
    detail: str | None = None,
    result: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a task's quarantine entry AND leave a record on its own work item.

    The phase record is the run's memory; the item file is the task's own, so
    the next run — which may not even resume this one — still sees why the
    last attempt didn't land. A task built with no file behind it (as tests
    do) or a store that refuses the write loses the item-side memory, never
    the quarantine entry itself.

    `kind` is the closed set from docs/design/observed-record.md §3. `infra`
    reaches here three ways: the caller's own branch at the `_open_phase_worktree`
    failure site, which marks the phase `phase_failed_to_start` before this
    function ever sees a task; `_execute`, when the apply arm itself raises
    rather than reports; and a non-build node raising mid-round, where the patch
    a later node was handed is kept rather than discarded. `unverified` is an
    approved patch the validator would not sign off, kept for the same reason.
    Both carry `patch_kept: True` on both the entry and the attempt. `infra` from
    `_execute` names the BUILD's already-approved patch already on the task
    record, not a resumable arm session: `auto_apply`'s `runner.run` call carries
    no `thread=`, so nothing about the failed arm call itself survives to be
    resumed.

    `detail`, when given, is stored as the attempt's own `reason` in place of
    the terse one on `entry` — the next build's carried-forward brief gets the
    fuller text, the printed quarantine line stays short.

    With a store, the attempt and the task record also carry a cause: the
    deterministic rule first, the cheap classifier only when it has no answer.
    `result`, the fix loop's own record of a refused build, supplies the
    classifier's arbitration text; without it the quarantine reason is the
    text. A classifier failure records `unknown` and never blocks the
    quarantine. The session limit is not a failure of the task: it propagates
    before anything is written, so the run pauses with no attempt recorded.
    """
    stale = _fenced(ctx)
    if stale is not None:
        # A stale leader records no attempt anywhere, so it is not an attempt either.
        return {"id": task, "phase": phase, "grain": "task", "reason": f"{stale}; not recorded: {reason}", "kind": "no_work"}
    # Before any write, so a `LimitStop` from the classifier leaves nothing half recorded.
    judged = _cause_of(ctx.runner, kind, reason, result, task) if ctx.store is not None else None
    patch_kept = kind in ("unverified", "infra")
    entry: dict[str, Any] = {"id": task, "phase": phase, "grain": "task", "reason": reason, "kind": kind}
    if patch_kept:
        entry["patch_kept"] = True
    ts = _now()
    path = (by_id.get(task) or {}).get("path")
    if path:
        with contextlib.suppress(WorkStoreError, OSError):
            record_attempt(
                path,
                run=ctx.run_id,
                phase=phase,
                reason=detail or reason,
                kind=kind,
                ts=ts,
                **({"patch_kept": True} if patch_kept else {}),
            )
    if ctx.store is not None and judged is not None:
        # The store's attempt exists whether or not the item has a file behind it.
        cause, cause_why = judged
        seq = _next_attempt_seq(ctx.store, ctx.run_id, task)
        ctx.store.record_attempt(
            ctx.run_id, task, seq, phase, kind, detail or reason, ts, epoch=ctx.epoch, cause=cause, cause_why=cause_why
        )
        _record_task_cause(ctx, phase, task, cause, cause_why)
    return entry


def _cause_of(runner: Any, kind: str, reason: str, result: Mapping[str, Any] | None, task: str) -> tuple[str, str]:
    """(cause, why): the rule first, then the model. Only `LimitStop` escapes: the account's limit is not the task's cause."""
    ruled = classify_cause(kind, reason)
    if ruled is not None:
        return ruled, f"rule: kind {kind}"
    reasoning, claims = cause_evidence(reason, result)
    try:
        return classify_with_model(runner, reasoning, claims, task=task)
    except LimitStop:
        raise
    except Exception as exc:
        return "unknown", one_line(f"classifier failed: {type(exc).__name__}: {exc}")


def _record_task_cause(ctx: _Ctx, phase: str, task: str, cause: str, cause_why: str) -> None:
    """Put the cause on this run's saved result, which mirrors it into the store row: file and row stay equal.

    A task with no saved result gets a minimal result-shaped one. A failure is a warning, never a block.
    """
    if ctx.store is None:
        return
    try:
        saved = load_result(ctx.runs_dir, ctx.run_id, phase, task) or {
            "ticket": task,
            "initiative": ctx.initiative_id,
            "phase": phase,
        }
        _save_result(ctx, {**saved, "cause": cause, "cause_why": cause_why}, phase=phase, task=task)
    except Exception as exc:
        _log.warning("cause for %s/%s/%s not recorded on the task record: %r", ctx.run_id, phase, task, exc)


def _without_cause(saved: Mapping[str, Any]) -> dict[str, Any]:
    """A saved result as a later run reuses it. Its cause names the run that quarantined it, not this one."""
    return {k: v for k, v in saved.items() if k not in ("cause", "cause_why")}


def _next_attempt_seq(store: Store, run_id: str, task: str) -> int:
    """The attempts already stored for (run, task), so a rerun of the same run id appends rather than collides."""
    mark = store.conn.dialect.placeholder
    row = store.conn.query_one(f"SELECT COUNT(*) FROM attempts WHERE run_id = {mark} AND task_id = {mark}", (run_id, task))
    return 0 if row is None else int(row[0])


LEASE_NAME = "chair"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _mirror_by(ctx: _Ctx) -> str:
    return f"epic-driver:{ctx.run_id}"


def _file_times(items: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """Task id to the ISO mtime of its work file; an item with no readable file has no entry."""
    times: dict[str, str] = {}
    for item in items:
        with contextlib.suppress(OSError):
            if item.get("path"):
                times[str(item["id"])] = datetime.fromtimestamp(Path(str(item["path"])).stat().st_mtime, UTC).isoformat()
    return times


def _upsert_row(store: Store, row: Mapping[str, Any]) -> None:
    upsert_work_item(
        store.conn, row["initiative"], row["task_id"], row["phase"], row["state"], row["needs"],
        row["updated_at"], row["updated_by"],
    )  # fmt: skip


def _repair_file_state(item: Mapping[str, Any], new_state: str) -> bool:
    """Rewrite only the `state:` line of the item's work file, bytes otherwise kept. False when it could not."""
    if not item.get("path"):
        return False
    path = Path(str(item["path"]))
    try:
        text = path.read_bytes().decode("utf-8")
        repaired = work_mirror.set_frontmatter_state(text, new_state)
        if repaired is None:
            return False
        if repaired != text:
            path.write_bytes(repaired.encode("utf-8"))
    except (OSError, UnicodeError):
        return False
    return True


def _mirror_read_store(ctx: _Ctx, items: Sequence[dict[str, Any]], rows: Sequence[Mapping[str, Any]]) -> None:
    """work_state: store. A row wins over its file: the file's state line is repaired and `items` follows the row.

    `items` is the driver's own copy, so the ready and needs checks that read it see the store's state. A task with
    no row is read from its file and upserted, as under files.
    """
    by_task = {row["task_id"]: row for row in rows}
    times = _file_times(items)
    for item in items:
        task, file_state = item["id"], item["state"]
        row = by_task.get(task)
        row_state = None if row is None else row["state"]
        decision = work_mirror.authority_decision("store", file_state, row_state)
        if decision == work_mirror.USE_STORE_REWRITE_FILE:
            if _repair_file_state(item, row_state):
                _log.warning("work file state rewritten from the store: task=%s old=%s new=%s", task, file_state, row_state)
            else:
                _log.warning("work file state not rewritable, store state used: task=%s file=%s", task, file_state)
            item["state"] = row_state
        elif decision == work_mirror.USE_FILE_AND_UPSERT:
            new = work_mirror.item_row(ctx.initiative_id, item, times.get(str(task)) or _now(), _mirror_by(ctx))
            _upsert_row(ctx.store, new)


def _mirror_read(ctx: _Ctx, items: Sequence[dict[str, Any]]) -> None:
    """Mirror the files' state into work_items. Under work_state files the files stay authoritative: `items` is never touched.

    Under work_state store a stored row wins: see `_mirror_read_store`. A ctx with no work_state reads as files.
    A store error is logged and swallowed here, at the edge, so it can never fail the run.
    """
    if ctx.store is None:
        return
    try:
        # Not a module-level import: harness/__init__ imports this module, so a top-level store_read import
        # loads store_traces early and `python -m harness.store_traces` then warns a second line on stderr.
        from harness import store_read

        rows = store_read.work_items(ctx.store.conn, ctx.initiative_id)
        if getattr(ctx, "work_state", "files") == "store":
            _mirror_read_store(ctx, items, rows)
            return
        upserts, disagreements = work_mirror.plan_mirror(
            ctx.initiative_id, items, rows, _file_times(items), _mirror_by(ctx), fallback_time=_now()
        )
        for row in upserts:
            _upsert_row(ctx.store, row)
        for d in disagreements:
            _log.warning(
                "work_items disagrees with the file: initiative=%s task=%s file_state=%s store_state=%s updated_by=%s",
                d["initiative"], d["task_id"], d["file_state"], d["store_state"], d["store_updated_by"],
            )  # fmt: skip
    except Exception as exc:
        _log.warning("work_items mirror (read) failed for %s: %s: %s", ctx.initiative_id, type(exc).__name__, exc)


def _store_authoritative(ctx: _Ctx) -> bool:
    return ctx.store is not None and getattr(ctx, "work_state", "files") == "store"


def _store_first(ctx: _Ctx, item: dict[str, Any], state: str) -> str | None:
    """work_state: store. Move `item` to `state` in the store, compare-and-set on the state the mover read.

    None when the store accepted, so the arm may now write the file. Otherwise the refusal reason: the arm must not run.
    A mismatch adopts the store's state into `item`, as `_mirror_read_store` does. A store error refuses the move.
    """
    from harness import store_work_state  # not module level: see `_mirror_read`

    task, expected = item.get("id"), str(item["state"])
    try:
        written = store_work_state.set_state(
            ctx.store.conn, ctx.initiative_id, str(task), state, _mirror_by(ctx), item.get("phase"), _now(), expected=expected
        )
    except Exception as exc:
        reason = f"state move refused, store write failed: {type(exc).__name__}: {exc}"
        _log.warning("%s: task=%s", reason, task)
        return reason
    if not isinstance(written, store_work_state.Mismatch):
        return None
    _, why = work_mirror.check_expected_state(expected, written.current)
    reason = f"state move refused: {why}"
    _log.warning("%s: task=%s", reason, task)
    if written.current is not None:
        item["state"] = written.current
    return reason


def _mirror_write(ctx: _Ctx, item: Mapping[str, Any] | None, state: str) -> None:
    """Upsert one task's work_items row after an arm wrote `state` to its file. Errors are logged, never raised.

    Under work_state store the row was already written by `_store_first`, before the file, so nothing is upserted.
    """
    if ctx.store is None or item is None or _store_authoritative(ctx):
        return
    try:
        _upsert_row(ctx.store, work_mirror.item_row(ctx.initiative_id, {**item, "state": state}, _now(), _mirror_by(ctx)))
    except Exception as exc:
        _log.warning("work_items mirror (write) failed for %s: %s: %s", item.get("id"), type(exc).__name__, exc)


def _fenced(ctx: _Ctx) -> str | None:
    """None when a leader-only write may proceed, else the `stale epoch` reason. No epoch, no fence."""
    if ctx.epoch is None or ctx.store is None:
        return None
    conn, name = ctx.store.conn, ctx.lease_name or LEASE_NAME
    if assert_epoch(conn, name, ctx.epoch, _now()):
        return None
    row = conn.query_one(f"SELECT epoch FROM leases WHERE name = {conn.dialect.placeholder}", (name,))
    held = "none" if row is None else row[0]
    return f"stale epoch: this driver holds epoch {ctx.epoch}, lease '{name}' is at epoch {held} or has expired"


def _task_record_mirror(
    store: Store | None, result: Mapping[str, Any], run_id: str, phase: str, task: str, ts: str
) -> tuple[str, str, str, dict[str, Any], str] | None:
    """Arguments for `Store.record_task_record`, or None with no store to mirror into.

    The record is the JSON round trip the file gets, so the row equals the saved file.
    """
    if store is None:
        return None
    return (run_id, phase, task, json.loads(json.dumps(dict(result), default=str)), ts)


def _save_result(ctx: _Ctx, result: Mapping[str, Any], *, phase: str, task: str) -> None:
    """Save the result file, then mirror it into the store. The file stays authoritative.

    A mirror failure is a warning, never a failed run. The file write is outside the try.
    """
    save_result(result, runs_dir=ctx.runs_dir, run_id=ctx.run_id, phase=phase, task=task)
    try:
        args = _task_record_mirror(ctx.store, result, ctx.run_id, phase, task, _now())
        if args is not None and ctx.store is not None and _fenced(ctx) is None:
            ctx.store.record_task_record(*args)
    except Exception as exc:
        _log.warning("task record for %s/%s/%s not mirrored into the store: %r", ctx.run_id, phase, task, exc)


def _record_phase(ctx: _Ctx, record: Mapping[str, Any]) -> None:
    if ctx.store is not None and _fenced(ctx) is None:
        row = {**record, "run_id": f"{ctx.run_id}:{record['phase']}", "ts": _now(), "principal": PRINCIPAL}
        ctx.store.record_phase(row, epoch=ctx.epoch)


def _record_tasks(
    ctx: _Ctx, phase: str, task_records: Sequence[Mapping[str, Any]], quarantined: Sequence[Mapping[str, Any]]
) -> None:
    """One row per task at its final state: the store keeps the first row for a (run, task)."""
    if ctx.store is None or _fenced(ctx) is not None:
        return
    states = {str(q["id"]): "quarantined" for q in quarantined if q.get("grain") == "task"}
    states.update({str(t["id"]): str(t.get("state") or t["status"]) for t in task_records})
    ts = _now()
    for task, state in sorted(states.items()):
        ctx.store.record_task(ctx.run_id, phase, task, state, ts, epoch=ctx.epoch)


def _refuse_stale(state: _Execution, *, phase: str, subject: str, slot: str, reason: str) -> None:
    """Quarantine what a stale leader tried to write. Not an attempt, so never through `_quarantine_task`."""
    task_grain = bool(subject) and slot != "rebase"
    entry = {
        "id": subject if task_grain else phase,
        "phase": phase,
        "grain": "task" if task_grain else "phase",
        "reason": reason,
        "kind": "no_work",
    }
    if entry not in state.quarantined:
        state.quarantined.append(entry)


def _frontmatter_block(body: str) -> str | None:
    """The text between the first pair of `---` fences, or None without one."""
    lines = body.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return "\n".join(lines[: index + 1])
    return None


def _ticket_amend_ramp(old_body: str, new_body: str) -> str:
    """`ramp` for a `ticket_amend` proposal, per docs/design/triage.md §2.

    Purely additive — only appended or repeated lines, frontmatter untouched —
    rides the kind's streak (`"eligible"`); any removed line, or any change to
    the frontmatter block, forces `"gated"` whatever the streak. `difflib`
    decides; no model call.
    """
    if _frontmatter_block(old_body) != _frontmatter_block(new_body):
        return "gated"
    removed = any(line.startswith("- ") for line in difflib.ndiff(old_body.splitlines(), new_body.splitlines()))
    return "gated" if removed else "eligible"


def _attempt_cap_reason(attempts: Sequence[Mapping[str, Any]]) -> str:
    """The refusal's reason, naming every earlier run so a person has the history."""
    history = "; ".join(f"{a.get('run')}: {a.get('reason')}" for a in attempts)
    return (
        f"attempt cap: {len(attempts)} earlier run(s) quarantined this task — {history}. "
        "Refusing a third run; a person decides."
    )


def _ready_view(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`items`, with a merged-but-unlanded parent presented as `done` for `needs`.

    `ready_tasks` satisfies a `needs` entry only against a literal `done`, and
    nothing in a run ever writes that — `cox runs land` does. Without this, a
    dependent phase's task would never become ready until a human landed the
    parent, even though the parent's code is already sitting on the phase
    branch stack the dependent builds from. Only a task that actually merged
    (`item["merged"]`) is presented this way; an escalated or conflicted
    parent stays `approved`, so its dependent still waits, unchanged. The
    view is read-only and local to this call — `items` itself, and every
    other read anywhere else, still shows `approved`.
    """
    return [
        {**item, "state": "done"} if item.get("state") == "approved" and item.get("merged") else item
        for item in items
    ]


def _run_phase(
    ctx: _Ctx,
    *,
    initiative: Mapping[str, Any],
    phase: str,
    parent: str | None,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    """One phase: branch, fan out, check, validate, gate, execute, record."""
    branch = ctx.phase_branch(phase)
    base_ref = ctx.phase_branch(parent) if parent else ctx.default_ref
    quarantined: list[dict[str, Any]] = []
    record: dict[str, Any] = {
        "phase": phase,
        "parent": parent,
        "branch": branch,
        "base": base_ref,
        "status": "partial",
        "rebased": False,
        "task_records": [],
        "quarantined": quarantined,
        "batch": [],
    }

    # A stale leader opens no branch and builds nothing.
    stale_start = _fenced(ctx)
    if stale_start:
        return _stale_phase(record, stale_start)

    ok, detail, reused = _open_phase_worktree(ctx, phase, base_ref)
    if not ok:
        record["status"] = "blocked"
        record["reason"] = f"the phase worktree could not be created: {detail}"
        record["phase_failed_to_start"] = record["reason"]
        quarantined.append({"id": phase, "phase": phase, "grain": "phase", "reason": record["reason"]})
        return record
    record["reused_branch"] = reused

    # Decided before a single task builds: a reused branch behind its base is
    # either recreated (nothing of its own to lose) or blocked (something is).
    head_moved = reused and _parent_head_moved(ctx, phase, base_ref)
    done_tasks = {str(i["id"]) for i in items if i.get("state") == "done"}
    has_own_commits = head_moved and _phase_branch_has_unlanded_commits(ctx, phase, done_tasks)
    action = branch_action(reused, head_moved, has_own_commits)

    if action == "block":
        # The proposal the quiet path below would have built for the same
        # staleness — filed here, unchanged, so the gate has it to decide on.
        record["batch"].append(
            proposal(
                ctx.cartridge,
                kind="stack_rebase",
                target=branch,
                evidence=[
                    {"check": "merge-base --is-ancestor", "output": f"{base_ref} is NOT an ancestor of {branch}"},
                    {"check": "stacked on", "output": f"{branch} was branched from {base_ref}, whose head has moved"},
                ],
                rationale=(
                    f"'{base_ref}' moved since '{branch}' was created, so this phase — and "
                    "everything stacked above it — is sitting on ground that is no longer there"
                ),
                suggested_action=f"rebase {branch} onto {base_ref}",
            )
        )
        record["status"] = "blocked"
        record["reason"] = (
            f"phase branch {branch} is behind {base_ref} and carries its own commits; its tasks "
            "would build against a base missing landed work — rebase it through the gate"
        )
        quarantined.append({"id": phase, "phase": phase, "grain": "phase", "reason": record["reason"]})
        # Release it, or the next run cannot open this branch at all.
        _git("-C", str(ctx.phase_worktree(phase)), "checkout", "--detach", "-q")
        return record

    if action == "recreate":
        stale_recreate = _fenced(ctx)
        if stale_recreate:
            return _stale_phase(record, stale_recreate)
        _git("-C", str(ctx.phase_worktree(phase)), "checkout", "--detach", "-q")
        _git("-C", str(ctx.repo), "worktree", "remove", "--force", str(ctx.phase_worktree(phase)))
        _git("-C", str(ctx.repo), "branch", "-D", branch)
        ok, detail, reused = _open_phase_worktree(ctx, phase, base_ref)
        if not ok:
            record["status"] = "blocked"
            record["reason"] = f"the phase worktree could not be recreated: {detail}"
            record["phase_failed_to_start"] = record["reason"]
            quarantined.append({"id": phase, "phase": phase, "grain": "phase", "reason": record["reason"]})
            return record
        record["reused_branch"] = reused
        record["recreated"] = f"phase branch {branch} recreated from {base_ref}: its commits already landed there via squash-merge"

    # A runner whose nodes can read the world reads THIS phase's branch — not
    # whatever the repository happens to have checked out. Phase N+1 stacks on
    # N, so a build that read the default branch would be patching ground that
    # is no longer there. Duck-typed: the API runner has no such attribute and
    # nothing to point anywhere.
    if hasattr(ctx.runner, "repo_dir"):
        ctx.runner.repo_dir = ctx.phase_worktree(phase)
    if hasattr(ctx.runner, "repo_digest"):
        ctx.runner.repo_digest = build_digest(ctx.phase_worktree(phase)) or None
    if hasattr(ctx.runner, "check_commands"):
        ctx.runner.check_commands = [str(c.get("cmd")) for c in ctx.checks if c.get("cmd")]

    # A rebase is a WRITE, so it is a proposal like any other and joins this
    # phase's gate batch rather than happening quietly on the way past.
    rebase: dict[str, Any] | None = None
    if reused and _parent_head_moved(ctx, phase, base_ref):
        rebase = proposal(
            ctx.cartridge,
            kind="stack_rebase",
            target=branch,
            evidence=[
                {"check": "merge-base --is-ancestor", "output": f"{base_ref} is NOT an ancestor of {branch}"},
                {"check": "stacked on", "output": f"{branch} was branched from {base_ref}, whose head has moved"},
            ],
            rationale=(
                f"'{base_ref}' moved since '{branch}' was created, so this phase — and "
                "everything stacked above it — is sitting on ground that is no longer there"
            ),
            suggested_action=f"rebase {branch} onto {base_ref}",
        )

    # A dropped task is terminal like a done one: it never gets rebuilt or
    # re-reviewed, and never blocks the phase behind it.
    # A need on a task in another initiative is met by the state the load
    # recorded under `initiative["foreign"]`; without it that task is never ready.
    foreign = initiative.get("foreign") or {}
    all_ready = [
        item
        for item in workstore.ready_tasks(_ready_view(items), phase=phase, foreign=foreign)
        if item.get("state") != "dropped"
    ]

    # A third run of the same task is refused outright rather than tried
    # again — quarantined here, plainly, never through `_quarantine_task`,
    # because a refusal is not an attempt and must not grow the count it is
    # enforcing.
    # An `infra` attempt is the apply arm's own failure, not the task's, so it
    # must not grow the count the cap enforces.
    attempts_by_id = {
        str(item["id"]): [a for a in (item.get("attempts") or []) if a.get("kind") != "infra"] for item in all_ready
    }
    # A capped task is launched into `triage` (docs/design/triage.md §1) rather
    # than quarantined outright: the graph classifies the attempt record and
    # emits by class, and only a class `triage` could not turn into a write —
    # or a repeated `(class, diagnosis)` it refuses to clear twice — still ends
    # up quarantined here, plainly, never through `_quarantine_task`.
    triage_batch: list[dict[str, Any]] = []
    capped_ids = sorted(task_id for task_id, attempts in attempts_by_id.items() if len(attempts) >= ATTEMPT_CAP)
    if capped_ids:
        all_ready_by_id = {str(item["id"]): item for item in all_ready}
        triaged, _, triage_failures = invoke_graphs(
            [
                Invocation(
                    id=task_id,
                    graph=TRIAGE,
                    args={
                        "cartridge": ctx.cartridge,
                        "date": ctx.date,
                        "work_item": all_ready_by_id[task_id],
                        "attempts": attempts_by_id[task_id],
                    },
                )
                for task_id in capped_ids
            ],
            specs=ctx.specs,
            runner=ctx.runner,
            run_id=f"{ctx.run_id}:{phase}",
            max_parallel=ctx.max_parallel,
        )
        for failure in triage_failures:
            task_id = failure.split(":", 1)[0]
            reason = f"{_attempt_cap_reason(attempts_by_id[task_id])} triage itself failed: {failure}"
            quarantined.append({"id": task_id, "phase": phase, "grain": "task", "reason": reason, "kind": "no_work"})
            print(f"  attempt cap: {task_id} refused; triage could not run ({failure})")

        for result in triaged:
            task_id = str(result.get("run_id", "")).rsplit(":", 1)[-1]
            attempts = attempts_by_id[task_id]
            if result.get("escalated"):
                reason = (
                    f"{_attempt_cap_reason(attempts)} triage repeats a prior "
                    f"({result.get('class')!r}, {result.get('diagnosis')!r}) diagnosis; escalating to a person."
                )
                quarantined.append({"id": task_id, "phase": phase, "grain": "task", "reason": reason, "kind": "no_work"})
                print(f"  attempt cap: {task_id} launched triage, which escalated a repeated diagnosis")
                continue

            emitted = dict(result.get("emit") or {})
            if "kind" not in emitted:
                reason = (
                    f"{_attempt_cap_reason(attempts)} triage classified this as "
                    f"{result.get('class')!r} with no direct write; a person decides."
                )
                quarantined.append({"id": task_id, "phase": phase, "grain": "task", "reason": reason, "kind": "no_work"})
                print(f"  attempt cap: {task_id} launched triage -> {result.get('class')} (no proposal)")
                continue

            if emitted["kind"] == "ticket_amend":
                old_body = str(all_ready_by_id[task_id].get("body") or "")
                new_body = f"{old_body}\n\n{emitted.get('suggested_action', '')}".rstrip()
                emitted["ramp"] = _ticket_amend_ramp(old_body, new_body)
            triage_batch.append(emitted)
            print(f"  attempt cap: {task_id} launched triage -> {result.get('class')} ({emitted['kind']})")
    ready = [item for item in all_ready if len(attempts_by_id[str(item["id"])]) < ATTEMPT_CAP]

    by_id = {str(item["id"]): item for item in items}
    if hasattr(ctx.runner, "verify_by_task"):
        ctx.runner.verify_by_task = {**ctx.runner.verify_by_task, **{t: _verify_of(by_id, t) for t in by_id}}
    results: list[dict[str, Any]] = []

    # Resume: a task whose earlier run already produced an approved patch is
    # reused at no cost. Applying and checking it below is unchanged, so a
    # patch that no longer fits the phase branch quarantines on its own merits.
    reused: list[dict[str, Any]] = []
    to_run = list(ready)
    if ctx.resume_from:
        to_run = []
        for task in ready:
            saved = load_result(ctx.runs_dir, ctx.resume_from, phase, str(task["id"]))
            if reusable(saved):
                reused.append(_without_cause(saved))
                print(f"  reused {task['id']} from {ctx.resume_from} (approved patch, no model call)")
            else:
                to_run.append(task)
    record["reused_tasks"] = [str(r.get("ticket")) for r in reused]

    # A task whose item asks for a build budget above the cartridge's cap
    # never reaches the model at all — refused outright, plainly, the same
    # way the attempt cap above is: never through `_quarantine_task`, because
    # this is not an attempt either and must not grow the count that gates it.
    cap = (ctx.cartridge.get("policy") or {}).get("build_budget_usd_max")
    # Item order, not set iteration order: a set's order comes from the
    # string hash seed, which is process environment, not a declared input,
    # so two runs over the same items could otherwise record and print the
    # refusals in a different order each time.
    over_budget = [
        task for task in to_run
        if task.get("budget_usd") is not None and cap is not None and task["budget_usd"] > cap
    ]
    for task in over_budget:
        task_id = str(task["id"])
        quarantined.append({
            "id": task_id, "phase": phase, "grain": "task", "kind": "no_work",
            "reason": (
                f"budget_usd {task['budget_usd']} exceeds the cartridge cap "
                f"build_budget_usd_max {cap} (a per build call ceiling)"
            ),
        })
        print(f"  build budget: {task_id} refused (budget_usd {task['budget_usd']} > cap {cap})")
    over_budget_ids = {str(task["id"]) for task in over_budget}
    runnable = [task for task in to_run if str(task["id"]) not in over_budget_ids]

    # A task the driver quarantined before carries its reasons — and, when one
    # was saved, its last patch — into the ticket body, so the planner and the
    # builder both see what already failed and why, next to the critique.
    # Offered as a reference; nothing here approves it.
    patches_for_attempt: dict[str, str | None] = {
        str(task["id"]): ((load_result(ctx.runs_dir, task["attempts"][-1]["run"], phase, str(task["id"])) or {})
                           .get("build") or {}).get("patch")
        for task in runnable
        if task.get("attempts")
    }

    if runnable:
        results, _, failures = invoke_graphs(
            [
                _lifecycle_invocation(
                    ctx,
                    task,
                    body=_carry_forward(
                        task.get("body") or "",
                        list(task.get("attempts") or []),
                        patches_for_attempt.get(str(task["id"])),
                        limit=PATCH_FOR_VALIDATION_CHARS,
                    ),
                    fix_attempts=ctx.fix_attempts,
                )
                for task in runnable
            ],
            specs=ctx.specs,
            runner=ctx.runner,
            run_id=f"{ctx.run_id}:{phase}",
            max_parallel=ctx.max_parallel,
        )
        # A child's failure is a quarantined task, not a failed swarm — the
        # policy `invoke_graphs` names continue-and-quarantine.
        for failure in failures:
            quarantined.append(
                _quarantine_task(
                    ctx, by_id, phase=phase, task=failure.split(":", 1)[0], reason=failure, kind="no_work"
                )
            )
    # Every result — fresh or reused — is saved under THIS run, so the next
    # resume has one place to look and the record of what ran is complete.
    results = [*reused, *results]
    for result in results:
        # `cox runs land`/`recover` resolve a phase branch as `epic/<initiative>/<phase>`;
        # without these two fields on the saved record, neither command can find it.
        result.setdefault("initiative", ctx.initiative_id)
        result.setdefault("phase", phase)
        _save_result(ctx, result, phase=phase, task=str(result.get("ticket")))

    built: dict[str, dict[str, Any]] = {}
    surviving: list[str] = []
    escalated: set[str] = set()

    for result in sorted(results, key=lambda r: str(r.get("ticket"))):
        task = str(result.get("ticket"))

        # A non-build node raised mid-round: review, adversary, arbitrate,
        # validate or handoff never reached a verdict, so nothing here was
        # judged and `_unapproved` has nothing to read. The patch a later
        # node was handed is kept rather than lost to the traceback.
        failed_node = result.get("failed_node")
        if failed_node is not None:
            patch = str((result.get("build") or {}).get("patch") or "")
            detail = str(result.get("failed_node_reason") or f"node '{failed_node}' failed")
            # Prefixed so `is_harness_fault` recognises it: this task's
            # quarantine never went through `built[task]`, so the shared
            # outcome loop below reads no verdicts for it and would otherwise
            # call `task_outcome` "rejected" — the same label a patch two
            # reviewers actually declined gets, for a patch nothing judged.
            reason = f"{HARNESS_FAULT_PREFIX} {detail}"
            record["task_records"].append(
                {
                    "id": task,
                    "phase": phase,
                    "branch": ctx.scratch_branch(task),
                    "evidence": [{"check": "fix_loop", "output": reason}],
                    "quarantine": reason,
                    "governance_hits": [],
                    "draft": None,
                    "merged": False,
                    "status": "quarantined",
                    "build": {"patch": patch},
                    "failed_node": failed_node,
                }
            )
            quarantined.append(_quarantine_task(ctx, by_id, phase=phase, task=task, reason=reason, kind="infra"))
            continue

        # The loop's refusal is the answer, and it is free. Applying a patch the
        # reviewers rejected costs a worktree, a check run and both validators
        # before anything says no, and what finally says no is a validator
        # reading a stale verdict rather than the loop that actually decided.
        refused = _unapproved(result)
        if refused is not None:
            record["task_records"].append(
                {
                    "id": task,
                    "phase": phase,
                    "branch": ctx.scratch_branch(task),
                    "evidence": [{"check": "fix_loop", "output": refused}],
                    "quarantine": refused,
                    "governance_hits": [],
                    "draft": None,
                    "merged": False,
                    "status": "quarantined",
                }
            )
            quarantined.append(
                _quarantine_task(ctx, by_id, phase=phase, task=task, reason=refused, kind="refused", result=result)
            )
            continue

        build = _build_task(ctx, phase=phase, task=task, result=result, verify=_verify_of(by_id, task))
        final, build = _refix(ctx, by_id, phase=phase, task=task, result=result, build=build)
        build["result"] = final
        built[task] = build

        # Evidence first, escalation second — the same order `cli.py` uses, and
        # for the same reason: the gate should see the tests' opinion of a
        # governance change too.
        for item in final.get("proposals") or []:
            if item.get("kind") in _DRAFT_KINDS:
                item.setdefault("evidence", []).extend(build["evidence"])

        # A change to the rules is not whatever kind the graph called it. This
        # runs after emission and before the policy split, which is the only
        # window where no streak on a mundane kind can carry a governance edit
        # past the gate.
        build["proposals"], hits = escalate_self_modification(
            final.get("proposals") or [],
            patch=str((final.get("build") or {}).get("patch") or ""),
            cartridge=ctx.cartridge,
            ledger_path=ctx.ledger_path,
        )
        if hits:
            escalated.add(task)
            build["governance_hits"] = hits

        record["task_records"].append(
            {
                "id": task,
                "phase": phase,
                "branch": build["branch"],
                "evidence": build["evidence"],
                "quarantine": build.get("quarantine"),
                # A quarantined task never reaches `surviving` below, so this is
                # the only task record it gets — the per-check results have to
                # land here too, not only on the `phase_state` a survivor's
                # validator sees.
                "change_facts": {
                    **(build["result"].get("change_facts") or {}),
                    **({"checks": build["checks"]} if "checks" in build else {}),
                },
                "governance_hits": hits,
                "draft": None,
                "merged": False,
                "status": "quarantined" if build.get("quarantine") else "built",
                "lint_fixed": build.get("lint_fixed", False),
                "lint_fix_checks": build.get("lint_fix_checks", []),
                **({"refix": build["refix"]} if build.get("refix") else {}),
            }
        )
        if build.get("quarantine"):
            reason = build["quarantine"]
            # A harness fault is not an attempt: the build was never actually
            # tried, so recording one here would burn the same two-strike cap
            # a real failure does. Mirrors the attempt-cap refusal above,
            # which quarantines without ever calling `_quarantine_task`.
            if is_harness_fault(reason):
                quarantined.append({"id": task, "phase": phase, "grain": "task", "reason": reason, "kind": "no_work"})
            else:
                # This branch is only reached for a task the fix loop already
                # approved — `_unapproved` above quarantines anything else as
                # `refused` before a worktree is even opened. A check still
                # failing here, after the lint_fix step had its chance, is a
                # non-functional finding on an approved patch, not grounds to
                # discard it: `unverified` keeps it, same as `validate_chunk`.
                detail = check_feedback(build.get("checks") or []) or None
                quarantined.append(
                    _quarantine_task(
                        ctx, by_id, phase=phase, task=task, reason=reason, kind="unverified", detail=detail
                    )
                )
            continue
        surviving.append(task)

    # ── validation, between the fan-out and the merges ───────────────────────
    chunk_by_task: dict[str, dict[str, Any]] = {}
    verdict: dict[str, Any] | None = None
    validated = "validate_phase" in ctx.bound

    if validated and results:
        phase_state = {
            "phase": {"id": phase, "goal": _phase_goal(initiative, phase)},
            "tasks": [
                {
                    "id": task,
                    "title": str((by_id.get(task) or {}).get("title") or task),
                    # The work store's own prose, never the builder's summary of
                    # what it did. See `graphs/delivery/phase_validate.py`: a
                    # validator handed the owner's account is reviewing a
                    # recollection, and the graph strips one if it arrives.
                    "description": str((by_id.get(task) or {}).get("body") or ""),
                    "evidence": built[task]["evidence"],
                    "change_facts": {
                        **(built[task]["result"].get("change_facts") or {}),
                        **({"checks": built[task]["checks"]} if "checks" in built[task] else {}),
                    },
                    "review_verdict": str((built[task]["result"].get("review") or {}).get("verdict") or ""),
                    # The patch is machine evidence — the diff git applied — not
                    # the builder's account of it. A validator without it said,
                    # in its own words, that it could not verify anything.
                    "patch": str((built[task]["result"].get("build") or {}).get("patch") or "")[:PATCH_FOR_VALIDATION_CHARS],
                    # Where `validate_chunk`'s needs_evidence, if any, gets
                    # read from — still on disk here, since validation runs
                    # before the merges that would retire it.
                    "worktree": str(ctx.task_worktree(phase, task)),
                }
                for task in surviving
            ],
            "quarantined": [{"id": q["id"], "reason": q["reason"]} for q in quarantined],
        }
        validations, _, failures = invoke_graphs(
            [
                Invocation(
                    id=f"{VALIDATE}:{phase}",
                    graph=VALIDATE,
                    args={
                        "date": ctx.date,
                        "cartridge": ctx.cartridge,
                        "phase_state": phase_state,
                        "reader": _read_evidence_file,
                    },
                )
            ],
            specs=ctx.specs,
            runner=ctx.runner,
            run_id=ctx.run_id,
            max_parallel=1,
        )
        if validations:
            record["chunk_verdicts"] = list(validations[0].get("chunk_verdicts") or [])
            verdict = record["phase_verdict"] = dict(validations[0].get("phase_verdict") or {})
            chunk_by_task = {str(v.get("task")): v for v in record["chunk_verdicts"]}
        else:
            # `validate_phase` raised for the WHOLE batch, not a single task —
            # `chunk_by_task` stays empty, and the loop below only quarantines
            # a task whose chunk verdict is present AND unsatisfied, so every
            # surviving task reaches the gate with no verdict at all rather
            # than a per-task `infra` quarantine. That is a real gap of the
            # same shape this ticket closes for a single task's node calls,
            # but closing it here means deciding what a WHOLE PHASE does when
            # its one validation call fails — hold every task, or requarantine
            # the batch — which this ticket's per-task scope does not cover.
            record["reason"] = f"the validator failed: {'; '.join(failures)}"

    # An unsatisfied chunk verdict quarantines its task BEFORE the gate. A task
    # the validator says did not do what it said is not a task whose merge
    # should be up for a decision.
    for task in list(surviving):
        chunk = chunk_by_task.get(task)
        if chunk is not None and not chunk.get("satisfied"):
            surviving.remove(task)
            gaps = ", ".join(chunk.get("gaps") or []) or str(chunk.get("reasoning", ""))
            quarantined.append(
                _quarantine_task(
                    ctx, by_id, phase=phase, task=task,
                    reason=f"validate_chunk unsatisfied: {gaps}", kind="unverified",
                )
            )
            for task_record in record["task_records"]:
                if task_record["id"] == task:
                    task_record["status"] = "quarantined"
                    task_record["quarantine"] = f"validate_chunk unsatisfied: {gaps}"

    # ── the phase's one gate batch, in task-id order ─────────────────────────
    batch, slots = _build_batch(
        ctx,
        phase=phase,
        surviving=surviving,
        built=built,
        escalated=escalated,
        chunk_by_task=chunk_by_task,
        rebase=rebase,
        by_id=by_id,
    )
    batch = batch + triage_batch
    record["batch"] = batch

    # ── policy, then the gate, then execution in batch order ────────────────
    auto, gated = split_by_policy(
        batch, cartridge=ctx.cartridge, ledger_path=ctx.ledger_path, provider_profile=ctx.provider_profile
    )
    auto_ids = {id(item) for item in auto}
    decisions, human_minutes = gate(gated, assume=ctx.assume)
    decided = {id(item): (decision, edited) for item, decision, edited in decisions}

    state = _Execution(landed={}, merged={}, moved={}, quarantined=quarantined)
    diffs: list[dict[str, Any]] = []

    for item in batch:
        slot, subject = slots.get(id(item), ("other", ""))
        if id(item) in auto_ids:
            applied, _ = _execute(ctx, item, slot=slot, subject=subject, phase=phase, state=state, by_id=by_id)
            # Auto-cleared: NO gate diff and NO ledger row. Autonomy is spent by
            # acting; a row here would let a kind ratchet itself up on its own
            # say-so, which is the self-report the ledger exists to disbelieve.
            record["rebased"] = record["rebased"] or (applied and slot == "rebase")
            continue

        decision, edited = decided.get(id(item), ("refused", False))
        applied = False
        if decision == "approved":
            applied, _ = _execute(ctx, item, slot=slot, subject=subject, phase=phase, state=state, by_id=by_id)
            record["rebased"] = record["rebased"] or (applied and slot == "rebase")
        # Built here rather than by `gate.apply_decisions`, which cannot know
        # about a branch this driver created: `applied` is what actually
        # happened, so an approved merge that conflicted records `skipped`.
        diffs.append(gate_diff(item, decision, applied=applied, edited=edited))

    # `_execute` can quarantine a task after its merge already succeeded — the
    # apply arm failing on its own bookkeeping, not the code — so that failure
    # must reach the task record here, or reconciliation below would see only
    # `merged=True` and report it `landed`.
    quarantined_by_task = {q["id"]: q["reason"] for q in state.quarantined if q.get("grain") == "task"}

    for task_record in record["task_records"]:
        task = task_record["id"]
        task_record["draft"] = ctx.draft_branch(phase, task) if state.landed.get(task) else None
        task_record["merged"] = bool(state.merged.get(task))
        if task in quarantined_by_task and not task_record.get("quarantine"):
            task_record["status"] = "quarantined"
            task_record["quarantine"] = quarantined_by_task[task]
        elif state.merged.get(task) is False and not task_record.get("quarantine"):
            task_record["status"] = "quarantined"
        elif not task_record["merged"] and not task_record.get("quarantine"):
            # Approved but its merge was never attempted — escalated to
            # `self_modification`, or any other reason `merge_stack` did not
            # run — so the task is not `done` yet, only `approved`.
            task_record["status"] = "approved"
        verdicts = (built.get(task) or {}).get("result") or {}
        task_record["outcome"] = task_outcome(
            str((verdicts.get("review") or {}).get("verdict") or "") or None,
            # A skipped arbiter is recorded as a string, not a mapping.
            str((verdicts.get("arbitration") if isinstance(verdicts.get("arbitration"), Mapping) else {}).get("verdict") or "") or None,
            task_record.get("quarantine"),
            # Merged AND quarantined only happens when `_execute`'s own
            # bookkeeping step failed after the code already landed — that is
            # not `landed` in the sense this outcome reports.
            task_record["merged"] and not task_record.get("quarantine"),
        )
        task_record["reason"] = task_record.get("quarantine")

    # An executed `state_move` is reflected in the driver's own copy of the work
    # so the next phase's tasks can become ready inside this run. `run_epic`
    # lands nothing itself — only `cox runs land` writes `done` (tools #153) —
    # so every moved task reads `approved` here, whether it merged into the
    # phase stack or not. `item["merged"]` is a second, internal-only field
    # (never returned to a caller) that `_ready_view` below reads to tell a
    # merged-but-unlanded parent from an escalated one that never reached the
    # stack at all.
    for task, moved in state.moved.items():
        if moved:
            merged = bool(state.merged.get(task))
            for item in items:
                if str(item["id"]) == task:
                    item["state"] = "approved"
                    item["merged"] = merged
            for task_record in record["task_records"]:
                if task_record["id"] == task:
                    task_record["state"] = "approved"

    _record_tasks(ctx, phase, record["task_records"], quarantined)

    record["status"], reason = _phase_status(
        verdict,
        validated=validated,
        ready=ready,
        quarantined=quarantined,
        items=items,
        phase=phase,
        landed=bool(surviving) and all(state.merged.get(task) for task in surviving),
        branch=branch,
    )
    if reason:
        record.setdefault("reason", reason)

    # docs/design/landing-model.md §5: one style_pass over the assembled phase
    # diff, run only once validate_phase itself has said the goal is met — a
    # trim is not a second opinion on completeness, it runs after that
    # question is already settled.
    if record["status"] == "complete" and verdict is not None and verdict.get("goal_met") and "style_pass" in ctx.bound:
        stale_trim = _fenced(ctx)
        outcome = f"refused: {stale_trim}" if stale_trim else _trim_phase(ctx, phase)
        if outcome is not None:
            record["trim"] = f"trim: {outcome}"

    # Release the phase branch. Git refuses to check one branch out in two
    # worktrees, and re-entrancy — a later run building on the branch this one
    # left — is a stated requirement, so the worktree keeps its files and gives
    # the branch back. Nothing here deletes work: the branches are the artifact.
    _git("-C", str(ctx.phase_worktree(phase)), "checkout", "--detach", "-q")

    totals = {
        "ready": len(ready),
        "completed": len(results),
        "surviving": len(surviving),
        "quarantined": sum(1 for q in quarantined if q.get("grain") == "task"),
        "auto_applied": len(auto),
        "gated": len(gated),
    }
    manifest = build_manifest(
        run_id=f"{ctx.run_id}:{phase}",
        ts=datetime.now(UTC).isoformat(),
        principal=PRINCIPAL,
        cartridge=ctx.cartridge,
        provider_profile=ctx.provider_profile,
        proposals=batch,
        gate_diffs=diffs,
        human_minutes=human_minutes,
        totals=totals,
    )
    record["totals"] = totals
    stale = _fenced(ctx)
    if stale is not None:
        return _stale_phase(record, stale)
    append_ledger(manifest, ledger_path=ctx.ledger_path)
    if ctx.store is not None:
        # Ledger rows are built in `core.manifest.append_ledger`; the store copies this phase's rows back by run id.
        for row in ledger.read(ctx.ledger_path):
            if row.get("run_id") == manifest["run_id"]:
                ctx.store.record_ledger(row, epoch=ctx.epoch)
        ctx.store.record_gate_decisions(ctx.run_id, phase, diffs, epoch=ctx.epoch)
    record["manifest"] = f"{ctx.run_id}:{phase}"
    record["manifest_record"] = manifest
    return record


def _stale_phase(record: dict[str, Any], reason: str) -> dict[str, Any]:
    """Block the phase on a stale epoch, so it neither reads complete nor unblocks its dependents."""
    record["status"] = "blocked"
    record["reason"] = reason
    record["quarantined"].append(
        {"id": record["phase"], "phase": record["phase"], "grain": "phase", "reason": reason, "kind": "no_work"}
    )
    return record


def _collected_ids(worktree: Path) -> set[str] | None:
    """`pytest --collect-only -q` in `worktree`, read back as node ids.

    `None` means unmeasurable: pytest could not launch (mirrors `run_checks`'s
    own `FileNotFoundError`/`OSError` handling, a harness fault rather than a
    reason to end the run), or it collected zero ids (a non-pytest repo, a
    collection error, a test-less tree). An empty set is not a passing floor,
    `set() <= anything` is true and would let any edit through untested.
    """
    try:
        proc = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q"], cwd=worktree, capture_output=True, text=True)
    except (FileNotFoundError, OSError):
        return None
    return collected_ids(proc.stdout) or None


def _trim_phase(ctx: _Ctx, phase: str) -> str | None:
    """Run `style_pass` once over the phase's assembled diff, gated on the coverage floor.

    §5 is literal: the diff is against `main` (`ctx.default_ref`), not the
    parent phase's branch — a phase branch is stacked on its unlanded parent
    within one run, so diffing anything narrower would hide exactly the
    cross-phase duplication and shims the extra sentence exists to catch.

    `None` means style_pass had nothing to offer — an empty patch is not a
    refusal, there was no trim to gate. Otherwise the outcome half of the
    phase record's `trim: ...` line (docs/design/landing-model.md §5). Before
    and after both collect from fresh worktrees of the phase branch, never
    `ctx.phase_worktree(phase)` itself — it still holds every task's own
    build worktree nested under it, and `pytest --collect-only` would walk
    straight into them. Every refusal path returns before the real phase
    worktree is touched, so a tripped floor leaves the branch exactly as
    `validate_phase` left it. Both scratch worktrees and the branches they
    were created on are torn down on every exit from the `with`, whether the
    floor held or not — `create_worktree` names them, nobody else deletes them.
    """
    worktree = ctx.phase_worktree(phase)
    branch = ctx.phase_branch(phase)
    ok, diff = _git("-C", str(ctx.repo), "diff", f"{ctx.default_ref}...{branch}")
    if not ok or not diff.strip():
        return None
    # The only direct `ctx.runner.run` call this module owns. No `task=` here:
    # `style_pass` runs once over the whole PHASE diff — every task's work
    # combined — so there is no single task id to stamp, and its ledger row
    # is `role: "style_pass"`, which `_trace_evidence`'s `role == "build"`
    # filter excludes regardless of `task_id` (see the test naming this call).
    result = ctx.runner.run(
        role="style_pass",
        tier="standard",
        schema=_STYLE_PASS_SCHEMA,
        prompt=f"{_STYLE_PASS_PROMPT}\n\nPhase: {phase}\n\nPhase diff:\n{diff}",
        context=list(ctx.cartridge.get("context") or []),
    )
    patch = str(result.get("patch") or "")
    if not patch.strip():
        return None

    before_branch = f"epic-trim-before/{ctx.run_id}/{phase}"
    after_branch = f"epic-trim-after/{ctx.run_id}/{phase}"
    outcome: str | None = None
    with TemporaryDirectory() as scratch:
        before_tree = Path(scratch) / "before"
        after_tree = Path(scratch) / "after"
        try:
            ok, _ = create_worktree(ctx.repo, before_tree, branch=before_branch, base=branch)
            before = _collected_ids(before_tree) if ok else None

            ok, _ = create_worktree(ctx.repo, after_tree, branch=after_branch, base=branch)
            applied = ok and apply_patch(patch, after_tree)[0]
            after = _collected_ids(after_tree) if applied else None

            if before is None or after is None:
                outcome = "refused (coverage floor)"
            else:
                checks_ok = all_passed(run_checks(after_tree, ctx.checks)) if ctx.checks else True
                if not (coverage_floor_holds(before, after) and checks_ok):
                    outcome = "refused (coverage floor)"
        finally:
            remove_worktree(ctx.repo, before_tree)
            remove_worktree(ctx.repo, after_tree)
            _git("-C", str(ctx.repo), "branch", "-D", before_branch)
            _git("-C", str(ctx.repo), "branch", "-D", after_branch)
    if outcome is not None:
        return outcome

    applied, _ = apply_patch(patch, worktree)
    if not applied:
        return "refused (coverage floor)"
    # `-u`, never `-A`: `worktree` still holds every task's own nested build
    # worktree (`task_worktree(phase, task) = phase_worktree(phase) / task`),
    # untracked from the phase branch's own point of view. `-A` would stage
    # each one as a gitlink at mode 160000, landing a pointer at a commit on
    # the harness-owned `agents/<run>/<task>` scratch namespace into the
    # commit this phase later squash-merges to main. `-u` restages only paths
    # already tracked on the branch — exactly what style_pass's patch touched.
    ok, _ = _git(*_IDENTITY, "-C", str(worktree), "add", "-u")
    if ok:
        ok, _ = _git(*_IDENTITY, "-C", str(worktree), "commit", "-q", "-m", f"epic {ctx.run_id}: trim {phase}")
    if not ok:
        return "refused (coverage floor)"
    removed = sum(1 for line in patch.splitlines() if line.startswith("-") and not line.startswith("---"))
    return f"{removed} lines removed"


def _phase_status(
    verdict: Mapping[str, Any] | None,
    *,
    validated: bool,
    ready: Sequence[Mapping[str, Any]],
    quarantined: Sequence[Mapping[str, Any]],
    items: Sequence[Mapping[str, Any]],
    phase: str,
    landed: bool,
    branch: str,
) -> tuple[str, str]:
    """Complete, or partial — and a phase unblocks its dependents only when complete.

    Blanket no, decided. A partially complete phase turns one quarantined task
    into a phase of work built on ground that is not there; the refinement
    (`quarantine_blocks_dependents`) is what the validator reports, and acting
    on it is a heavier ask of that role than anything else in the spec.

    An unbound `validate_phase` is not an approval. A team that binds no
    validator gets task completion and NO claim about phase completion, which
    is honest — so the phase is partial, and its dependents wait.

    A met goal is not sufficient either, and this is where open question 1 gets
    its answer: the verdict is about work that exists on branches, and the gate
    decides whether that work reaches the phase branch. If the merges were
    refused, the next phase would branch from a phase branch with nothing on it,
    so the phase is partial no matter how good the work was.
    """
    if not ready and not quarantined and all(
        item.get("state") in ("done", "dropped", "approved") for item in items if item.get("phase") == phase
    ):
        return "complete", "every task in the phase was already done"
    if not validated:
        return "partial", "no validator bound"
    if verdict is None:
        return "partial", "the validator produced no verdict"
    if not verdict.get("goal_met"):
        return "partial", str(verdict.get("reasoning") or "the phase goal was not met")
    if not landed:
        return "partial", f"the goal is met, but nothing was merged into {branch}"
    return "complete", ""


def _finding_entries(result: Mapping[str, Any], chunk: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Every (text, file) a review, adversary or validate verdict wrote for one task.

    Only a review finding's `file` is structured: REVIEW_SCHEMA requires it on
    every entry (`graphs/delivery/lifecycle_propose.py`), so it is read straight
    off rather than guessed at. An adversary objection and a validate gap or
    reasoning string carry no such field — `("text", "")` — and are matched on
    their own prose in `_cited_surface` below.
    """
    review = (result.get("review") or {}).get("findings") or []
    adversary = (result.get("adversary") or {}).get("objections") or []
    entries = [(str(f.get("detail") or ""), str(f.get("file") or "")) for f in review]
    entries += [(f"{o.get('claim') or ''} {o.get('why_wrong') or ''}".strip(), "") for o in adversary]
    entries += [(str(g), "") for g in chunk.get("gaps") or []]
    entries.append((str(chunk.get("reasoning") or ""), ""))
    return entries


def _cited_surface(text: str, file: str, surfaces: Sequence[str]) -> str | None:
    """The one entry of `surfaces` that `file` names outright, or that `text` quotes."""
    if file and file in surfaces:
        return file
    return next((s for s in surfaces if s and s in text), None)


def _consolidation_pairs(
    built: Mapping[str, Mapping[str, Any]],
    surviving: Sequence[str],
    chunk_by_task: Mapping[str, Mapping[str, Any]],
    by_id: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, str]]:
    """One entry per pair of surviving tasks a finding names as blocked on a file the other owns.

    `docs/design/work-shape.md` §4, quoted verbatim: "a review, adversary or
    validate finding stating that a build cannot satisfy its ticket without
    touching a file another ticket in the phase owns." §4 names no phrase to
    match — the fact it describes is OWNERSHIP, and `docs/design/work-shape.md`
    §3's coupling rule already computes that off each ticket's own `surfaces`
    list, so a finding qualifies here on the same evidence: the file it names
    is a `surfaces` entry of a DIFFERENT surviving task in this phase, never on
    a model's choice of words.
    """
    seen: set[frozenset[str]] = set()
    pairs: list[dict[str, str]] = []
    for task in surviving:
        entries = _finding_entries(built[task]["result"], chunk_by_task.get(task) or {})
        for other in surviving:
            if other == task or frozenset({task, other}) in seen:
                continue
            surfaces = [str(s) for s in (by_id.get(other) or {}).get("surfaces") or []]
            if not surfaces:
                continue
            for text, file in entries:
                cited = _cited_surface(text, file, surfaces)
                if cited is None:
                    continue
                seen.add(frozenset({task, other}))
                pairs.append({"task": task, "other": other, "file": cited, "text": text})
                break
    return pairs


def state_move_apply(path: str | None, state: str) -> dict[str, Any]:
    """The `apply` payload the code arm reads; empty when no path is known, so the gate falls back to the model arm."""
    return {"apply": {"path": str(path), "state": state}} if path else {}


def item_apply(path: str | None, item: Mapping[str, Any]) -> dict[str, Any]:
    """The `apply` payload for item_create and item_update; empty when no path is known."""
    return {"apply": {"path": str(path), "item": dict(item)}} if path else {}


def _build_batch(
    ctx: _Ctx,
    *,
    phase: str,
    surviving: Sequence[str],
    built: Mapping[str, Mapping[str, Any]],
    escalated: set[str],
    chunk_by_task: Mapping[str, Mapping[str, Any]],
    rebase: Mapping[str, Any] | None,
    by_id: Mapping[str, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[int, tuple[str, str]]]:
    """Everything this phase asks for, in task-id order, plus what each slot means.

    The slot map is by identity rather than by target string, because the same
    proposal objects travel through `split_by_policy` and `gate` unchanged and
    the driver has to know, at execution time, which branch a given proposal
    was about. Reparsing a target would be a second encoding of the same fact.
    """
    batch: list[dict[str, Any]] = []
    slots: dict[int, tuple[str, str]] = {}

    for task in surviving:
        for item in built[task]["proposals"]:
            batch.append(item)
            if item.get("kind") in _DRAFT_KINDS:
                slots[id(item)] = ("draft", task)

    for task in surviving:
        merge = proposal(
            ctx.cartridge,
            kind="merge_stack",
            target=f"{ctx.draft_branch(phase, task)} -> {ctx.phase_branch(phase)}",
            evidence=[
                *built[task]["evidence"],
                {
                    "check": "validate_chunk",
                    "output": (
                        f"satisfied — {chunk_by_task[task].get('reasoning')}"
                        if task in chunk_by_task
                        else "not bound; no chunk verdict was produced"
                    ),
                },
            ],
            rationale=f"{task} is reviewed, applied and checked on its own branch off {ctx.phase_branch(phase)}",
            suggested_action=f"merge {ctx.draft_branch(phase, task)} into {ctx.phase_branch(phase)} (no fast-forward)",
        )
        if task in escalated:
            # The patch touched governance, so the MERGE of that patch is a
            # governance write too. Escalating it here is what makes "its merge
            # is impossible to earn" true rather than merely intended.
            merged, _ = escalate_self_modification(
                [merge],
                patch=str((built[task]["result"].get("build") or {}).get("patch") or ""),
                cartridge=ctx.cartridge,
                ledger_path=ctx.ledger_path,
            )
            merge = merged[0]
        batch.append(merge)
        slots[id(merge)] = ("merge", task)

    if rebase is not None:
        batch.append(dict(rebase))
        slots[id(batch[-1])] = ("rebase", phase)

    for task in surviving:
        move = proposal(
            ctx.cartridge,
            kind="state_move",
            target=task,
            evidence=[
                {
                    "check": "review_charter verdict",
                    "output": str((built[task]["result"].get("review") or {}).get("verdict")),
                },
                *built[task]["evidence"],
            ],
            rationale=f"{task} was built, checked and reviewed in this run",
            # `approved`, never `done`: the apply arm writes the state this names, and only
            # `cox runs land` writes `done`, after the merge (tools #153).
            suggested_action=f"mark {task} approved",
        )
        move = {**move, **state_move_apply((by_id.get(task) or {}).get("path"), "approved")}
        batch.append(move)
        slots[id(move)] = ("state_move", task)

    for pair in _consolidation_pairs(built, surviving, chunk_by_task, by_id):
        task, other, file, text = pair["task"], pair["other"], pair["file"], pair["text"]
        ids = " and ".join(sorted((task, other)))
        # Propose only, per conventions.md's propose-don't-write posture: no
        # slots entry, so `_execute`'s default ("other", "") slot never merges
        # or moves anything on this proposal's account.
        batch.append(
            proposal(
                ctx.cartridge,
                kind="consolidate",
                target=ids,
                evidence=[{"check": "review/adversary/validate finding", "output": text}],
                rationale=f"{task} cannot satisfy its ticket without touching {file}, which {other} owns",
                suggested_action=f"merge {ids} into one ticket and mark the superseded one dropped",
            )
        )

    return batch, slots


@dataclass
class _Execution:
    """What actually happened, per task, as the batch executes."""

    landed: dict[str, bool]
    merged: dict[str, bool]
    moved: dict[str, bool]
    quarantined: list[dict[str, Any]]


def _execute(
    ctx: _Ctx,
    item: Mapping[str, Any],
    *,
    slot: str,
    subject: str,
    phase: str,
    state: _Execution,
    by_id: Mapping[str, dict[str, Any]],
) -> tuple[bool, str]:
    """Do what the gate — or the policy — cleared. Dispatch on the KIND, not the slot.

    The kind is what governs, and escalation rewrites it: a `draft_pr_create`
    whose patch touched governance arrives here as `self_modification`, falls
    through to the arm the cartridge names (`pr`, which has no executor here),
    and reports honestly that nothing happened. That is what makes an escalated
    task's merge impossible to earn rather than merely discouraged.
    """
    stale = _fenced(ctx)
    if stale is not None:
        _refuse_stale(state, phase=phase, subject=subject, slot=slot, reason=stale)
        return False, stale

    kind = item.get("kind")

    if kind == "draft_pr_create" and slot == "draft":
        # Landing the draft IS creating the branch. There is no forge arm here,
        # so the "draft PR" is a local branch until one exists — and a branch
        # nobody has opened has exactly the blast radius the taxonomy prices it
        # at: the cost of a wrong one is a branch nobody reads.
        draft = ctx.draft_branch(phase, subject)
        ok, detail = _git("-C", str(ctx.repo), "branch", draft, ctx.scratch_branch(subject))
        state.landed[subject] = ok
        return ok, detail or f"created {draft}"

    if kind == "merge_stack" and slot == "merge":
        # Task branch -> its parent PHASE branch, and nothing else. No path in
        # this driver merges to a default branch: `merge_main` is never emitted,
        # never executed, and unreachable from here at any comfort level and on
        # any streak. The swarm's output is branches and drafts.
        if not state.landed.get(subject):
            return False, "the draft did not land, so its merge is refused with it"
        draft = ctx.draft_branch(phase, subject)
        ok, detail = _git(
            *_IDENTITY, "-C", str(ctx.phase_worktree(phase)), "merge", "--no-ff", draft,
            "-m", f"epic {ctx.run_id}: merge {subject} into {phase}",
        )
        if not ok:
            _git("-C", str(ctx.phase_worktree(phase)), "merge", "--abort")
            state.quarantined.append(
                {"id": subject, "phase": phase, "grain": "task", "reason": f"merge conflict: {detail}"}
            )
        state.merged[subject] = ok
        return ok, detail

    if kind == "stack_rebase" and slot == "rebase":
        ok, detail = _rebase(ctx, phase, _rebase_base(item))
        if not ok:
            state.quarantined.append(
                {"id": phase, "phase": phase, "grain": "phase", "reason": f"rebase conflict: {detail}"}
            )
        return ok, detail

    # Everything else goes to the arm the cartridge names — the same call
    # `gate.apply_decisions` makes, because an apply arm is a role and the same
    # runner that ran the read-only nodes runs the write. The build itself was
    # already reviewed, adversaried and arbitrated; a `RunnerError` here is the
    # arm's own infrastructure failing, not the task, so it quarantines the
    # task as `infra` and lets the phase continue rather than crashing `run_epic`.
    if slot == "state_move" and _store_authoritative(ctx) and by_id.get(subject) is not None:
        # The store moves first; the arm writes the file's `state:` line only once the store has accepted.
        refused = _store_first(ctx, by_id[subject], "approved")
        if refused is not None:
            state.moved[subject] = False
            return False, refused
    try:
        applied, detail = auto_apply(dict(item), cartridge=ctx.cartridge, runner=ctx.runner)
    except LimitStop:
        raise  # the account's own limit, not this task's; never through `_quarantine_task`
    except RunnerError as exc:
        arm = apply_arm_for(item.get("kind"), ctx.cartridge)
        reason = f"apply arm '{arm}' raised {type(exc).__name__}: {exc}"
        state.quarantined.append(_quarantine_task(ctx, by_id, phase=phase, task=subject, reason=reason, kind="infra"))
        return False, reason
    if slot == "state_move":
        state.moved[subject] = applied
        if applied:
            _mirror_write(ctx, by_id.get(subject), "approved")
    return applied, detail


def _rebase_base(item: Mapping[str, Any]) -> str:
    """The ref a `stack_rebase` proposal names as the ground that moved.

    Read back off the proposal rather than recomputed, so what executes is the
    thing the gate was shown: a rebase onto a ref nobody approved is a different
    write from the one that was decided.
    """
    return str(item.get("suggested_action") or "").rsplit(" onto ", 1)[-1].strip()
