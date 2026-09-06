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
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core import workstore
from core.manifest import build_manifest, gate_diff, record_run
from core.workstore import WorkStoreError, record_attempt

from graphs._contract import proposal
from harness.autonomy import split_by_policy
from harness.checks import checks_evidence, is_harness_fault, quarantine_reason, repo_checks, run_checks
from harness.digest import build_digest
from harness.escalate import escalate_self_modification
from harness.gate import auto_apply, gate
from harness.invoke import Invocation, invoke_graphs
from harness.resume import load_result, reusable, save_result
from harness.worktree import apply_patch, create_worktree

__all__ = ["phase_order", "phase_parents", "run_epic"]

LIFECYCLE = "lifecycle"
VALIDATE = "validate"

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
    parents: dict[str, set[str]] = {phase: set() for phase in phase_of.values() if phase}
    for item in items:
        here = str(item.get("phase") or "")
        for need in item.get("needs") or []:
            there = phase_of.get(str(need))
            if here and there and there != here:
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
    """
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


def _build_task(ctx: _Ctx, *, phase: str, task: str, result: Mapping[str, Any]) -> dict[str, Any]:
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

    if ctx.checks:
        results = run_checks(worktree, ctx.checks)
        record["checks"] = results
        record["evidence"].extend(checks_evidence(results))
        reason = quarantine_reason(results)
        if reason:
            record["quarantine"] = reason
    return record


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
    verdict = str((result.get("review") or {}).get("verdict") or "") or "none recorded"
    counted = f" after {attempts} build attempt{'s' if attempts != 1 else ''}" if attempts else ""
    return (
        f"the fix loop stopped: {stopped}{counted}; the last review verdict was "
        f"'{verdict}' and no build was approved"
    )


# ── the driver ──────────────────────────────────────────────────────────────


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
    """
    repo = Path(repo)
    ok, head = _git("-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD")
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
        initiative_id=str(initiative.get("id")),
        # An unparented phase branches from the repository's current HEAD, read
        # once here so every phase in a run stacks on the same ground.
        default_ref=head.strip() if ok else "HEAD",
    )

    # The driver's own view of the work. `ready_tasks` answers from item state,
    # so an EXECUTED `state_move` has to be reflected here or the next phase's
    # tasks never become ready within this run. The arm remains the single
    # writer of the store on disk; this is the driver keeping its own copy
    # honest about what the arm just did.
    items = [dict(item) for item in initiative.get("items") or []]

    parents = phase_parents(items)
    ordered, cyclic = phase_order(parents)

    phases: list[dict[str, Any]] = []
    tasks: list[dict[str, Any]] = []
    quarantined: list[dict[str, Any]] = []
    proposals: list[dict[str, Any]] = []
    complete: set[str] = set()
    stacks_rebased = 0

    for phase in cyclic:
        phases.append(
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
            phases.append(
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
            phases.append(
                {
                    "phase": phase,
                    "status": "blocked",
                    "parents": parent_phases,
                    "reason": f"parent phase '{parent}' did not meet its goal; dependents do not run",
                }
            )
            continue

        record = _run_phase(ctx, initiative=initiative, phase=phase, parent=parent, items=items)
        # Threads live for a phase: every task's plan/build/retry is done by now.
        close = getattr(ctx.runner, "close", None)
        if callable(close):
            close()
        tasks.extend(record.pop("task_records"))
        quarantined.extend(record.pop("quarantined"))
        proposals.extend(record.pop("batch"))
        stacks_rebased += 1 if record.get("rebased") else 0
        phases.append(record)
        if record["status"] == "complete":
            complete.add(phase)

    return {
        "run_id": run_id,
        "date": date,
        "initiative": ctx.initiative_id,
        "phases": phases,
        "tasks": tasks,
        "quarantined": quarantined,
        "proposals": proposals,
        "totals": {
            "phases_complete": sum(1 for p in phases if p["status"] == "complete"),
            "phases_partial": sum(1 for p in phases if p["status"] == "partial"),
            "phases_blocked": sum(1 for p in phases if p["status"] == "blocked"),
            "tasks_quarantined": sum(1 for q in quarantined if q.get("grain") == "task"),
            "stacks_rebased": stacks_rebased,
        },
    }


def _quarantine_task(
    ctx: _Ctx,
    by_id: Mapping[str, dict[str, Any]],
    *,
    phase: str,
    task: str,
    reason: str,
) -> dict[str, Any]:
    """Build a task's quarantine entry AND leave a record on its own work item.

    The phase record is the run's memory; the item file is the task's own, so
    the next run — which may not even resume this one — still sees why the
    last attempt didn't land. A task built with no file behind it (as tests
    do) or a store that refuses the write loses the item-side memory, never
    the quarantine entry itself.
    """
    path = (by_id.get(task) or {}).get("path")
    if path:
        with contextlib.suppress(WorkStoreError, OSError):
            record_attempt(path, run=ctx.run_id, phase=phase, reason=reason, ts=datetime.now(UTC).isoformat())
    return {"id": task, "phase": phase, "grain": "task", "reason": reason}


def _attempt_cap_reason(attempts: Sequence[Mapping[str, Any]]) -> str:
    """The refusal's reason, naming every earlier run so a person has the history."""
    history = "; ".join(f"{a.get('run')}: {a.get('reason')}" for a in attempts)
    return (
        f"attempt cap: {len(attempts)} earlier run(s) quarantined this task — {history}. "
        "Refusing a third run; a person decides."
    )


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

    ok, detail, reused = _open_phase_worktree(ctx, phase, base_ref)
    if not ok:
        record["status"] = "blocked"
        record["reason"] = f"the phase worktree could not be created: {detail}"
        quarantined.append({"id": phase, "phase": phase, "grain": "phase", "reason": record["reason"]})
        return record
    record["reused_branch"] = reused

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

    all_ready = workstore.ready_tasks(items, phase=phase)

    # A third run of the same task is refused outright rather than tried
    # again — quarantined here, plainly, never through `_quarantine_task`,
    # because a refusal is not an attempt and must not grow the count it is
    # enforcing.
    attempts_by_id = {str(item["id"]): item.get("attempts") or [] for item in all_ready}
    for task_id, attempts in attempts_by_id.items():
        if len(attempts) < ATTEMPT_CAP:
            continue
        reason = _attempt_cap_reason(attempts)
        quarantined.append({"id": task_id, "phase": phase, "grain": "task", "reason": reason})
        print(f"  attempt cap: {task_id} refused ({len(attempts)} attempt(s) recorded)")
    ready = [item for item in all_ready if len(attempts_by_id[str(item["id"])]) < ATTEMPT_CAP]

    by_id = {str(item["id"]): item for item in items}
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
                reused.append(saved)
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
            "id": task_id, "phase": phase, "grain": "task",
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
                Invocation(
                    id=str(task["id"]),
                    graph=LIFECYCLE,
                    args={
                        "date": ctx.date,
                        "ticket": task["id"],
                        "ticket_title": task.get("title") or "",
                        "ticket_body": _carry_forward(
                            task.get("body") or "",
                            list(task.get("attempts") or []),
                            patches_for_attempt.get(str(task["id"])),
                            limit=PATCH_FOR_VALIDATION_CHARS,
                        ),
                        "cartridge": ctx.cartridge,
                        "surfaces": list(task.get("surfaces") or []),
                        "patterns": list(task.get("patterns") or []),
                        **({"fix_attempts": ctx.fix_attempts} if ctx.fix_attempts is not None else {}),
                        **({"build_budget_usd": task["budget_usd"]} if task.get("budget_usd") is not None else {}),
                    },
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
                _quarantine_task(ctx, by_id, phase=phase, task=failure.split(":", 1)[0], reason=failure)
            )
    # Every result — fresh or reused — is saved under THIS run, so the next
    # resume has one place to look and the record of what ran is complete.
    results = [*reused, *results]
    for result in results:
        save_result(result, runs_dir=ctx.runs_dir, run_id=ctx.run_id, phase=phase, task=str(result.get("ticket")))

    built: dict[str, dict[str, Any]] = {}
    surviving: list[str] = []
    escalated: set[str] = set()

    for result in sorted(results, key=lambda r: str(r.get("ticket"))):
        task = str(result.get("ticket"))

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
            quarantined.append(_quarantine_task(ctx, by_id, phase=phase, task=task, reason=refused))
            continue

        build = _build_task(ctx, phase=phase, task=task, result=result)
        build["result"] = result
        built[task] = build

        # Evidence first, escalation second — the same order `cli.py` uses, and
        # for the same reason: the gate should see the tests' opinion of a
        # governance change too.
        for item in result.get("proposals") or []:
            if item.get("kind") in _DRAFT_KINDS:
                item.setdefault("evidence", []).extend(build["evidence"])

        # A change to the rules is not whatever kind the graph called it. This
        # runs after emission and before the policy split, which is the only
        # window where no streak on a mundane kind can carry a governance edit
        # past the gate.
        build["proposals"], hits = escalate_self_modification(
            result.get("proposals") or [],
            patch=str((result.get("build") or {}).get("patch") or ""),
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
                "governance_hits": hits,
                "draft": None,
                "merged": False,
                "status": "quarantined" if build.get("quarantine") else "built",
            }
        )
        if build.get("quarantine"):
            reason = build["quarantine"]
            # A harness fault is not an attempt: the build was never actually
            # tried, so recording one here would burn the same two-strike cap
            # a real failure does. Mirrors the attempt-cap refusal above,
            # which quarantines without ever calling `_quarantine_task`.
            if is_harness_fault(reason):
                quarantined.append({"id": task, "phase": phase, "grain": "task", "reason": reason})
            else:
                quarantined.append(_quarantine_task(ctx, by_id, phase=phase, task=task, reason=reason))
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
                    "change_facts": dict(built[task]["result"].get("change_facts") or {}),
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
                _quarantine_task(ctx, by_id, phase=phase, task=task, reason=f"validate_chunk unsatisfied: {gaps}")
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
    )
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
            applied, _ = _execute(ctx, item, slot=slot, subject=subject, phase=phase, state=state)
            # Auto-cleared: NO gate diff and NO ledger row. Autonomy is spent by
            # acting; a row here would let a kind ratchet itself up on its own
            # say-so, which is the self-report the ledger exists to disbelieve.
            record["rebased"] = record["rebased"] or (applied and slot == "rebase")
            continue

        decision, edited = decided.get(id(item), ("refused", False))
        applied = False
        if decision == "approved":
            applied, _ = _execute(ctx, item, slot=slot, subject=subject, phase=phase, state=state)
            record["rebased"] = record["rebased"] or (applied and slot == "rebase")
        # Built here rather than by `gate.apply_decisions`, which cannot know
        # about a branch this driver created: `applied` is what actually
        # happened, so an approved merge that conflicted records `skipped`.
        diffs.append(gate_diff(item, decision, applied=applied, edited=edited))

    for task_record in record["task_records"]:
        task = task_record["id"]
        task_record["draft"] = ctx.draft_branch(phase, task) if state.landed.get(task) else None
        task_record["merged"] = bool(state.merged.get(task))
        if state.merged.get(task) is False and not task_record.get("quarantine"):
            task_record["status"] = "quarantined"

    # An executed `state_move` is reflected in the driver's own copy of the work
    # so the next phase's tasks can become ready inside this run.
    for task, done in state.moved.items():
        if done:
            for item in items:
                if str(item["id"]) == task:
                    item["state"] = "done"

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
    record_run(manifest, runs_dir=ctx.runs_dir, ledger_path=ctx.ledger_path)
    record["manifest"] = f"{ctx.run_id}:{phase}"
    record["totals"] = totals
    return record


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
        item.get("state") == "done" for item in items if item.get("phase") == phase
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


def _build_batch(
    ctx: _Ctx,
    *,
    phase: str,
    surviving: Sequence[str],
    built: Mapping[str, Mapping[str, Any]],
    escalated: set[str],
    chunk_by_task: Mapping[str, Mapping[str, Any]],
    rebase: Mapping[str, Any] | None,
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
            suggested_action=f"mark {task} done",
        )
        batch.append(move)
        slots[id(move)] = ("state_move", task)

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
) -> tuple[bool, str]:
    """Do what the gate — or the policy — cleared. Dispatch on the KIND, not the slot.

    The kind is what governs, and escalation rewrites it: a `draft_pr_create`
    whose patch touched governance arrives here as `self_modification`, falls
    through to the arm the cartridge names (`pr`, which has no executor here),
    and reports honestly that nothing happened. That is what makes an escalated
    task's merge impossible to earn rather than merely discouraged.
    """
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
    # runner that ran the read-only nodes runs the write.
    applied, detail = auto_apply(dict(item), cartridge=ctx.cartridge, runner=ctx.runner)
    if slot == "state_move":
        state.moved[subject] = applied
    return applied, detail


def _rebase_base(item: Mapping[str, Any]) -> str:
    """The ref a `stack_rebase` proposal names as the ground that moved.

    Read back off the proposal rather than recomputed, so what executes is the
    thing the gate was shown: a rebase onto a ref nobody approved is a different
    write from the one that was decided.
    """
    return str(item.get("suggested_action") or "").rsplit(" onto ", 1)[-1].strip()
