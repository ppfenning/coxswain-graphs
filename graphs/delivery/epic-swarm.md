# epic-swarm — specification

Runs a whole initiative to completion without supervision, and lands nothing.

`initiative-decompose` produces phases, tasks, and the edges between them.
`run_phase` already runs one phase's unblocked tasks at once. What is missing is
the thing above both: something that walks the phase graph, drives each phase in
dependency order, decides when a phase is actually finished, and keeps going
until the initiative is done or stuck.

The vocabulary is the one already in the work store: **initiative → phase →
task**. A task is the unit that gets a worktree and a draft PR. There is no
fourth level and no synonym for `task`.

## This is not a graph, and that matters

The contract already ruled on this, in `docs/GRAPH-CONTRACT.md`:

> Running a *phase* is not a graph: the shell runs `lifecycle-propose` once per
> unblocked task, concurrently. Sequence belongs to a graph; concurrency belongs
> to the I/O edge that already owns every side effect.

Everything that argument says about a phase driver says the same about a swarm
driver, only louder. This thing invokes graphs, waits on them, decides what to
invoke next from what came back, and does it concurrently. A graph is
`run(args, runner) -> dict` — pure, replayable, no clock, no disk. A driver that
blocks on futures is none of those things.

So `epic-swarm` is **a harness capability plus two roles and a write kind**, not
a fifth graph. It is specified here, beside the graphs it drives, because this is
where a reader looks for it — but it belongs in `harness/`, next to `phase.py`,
and it must not acquire a `SPEC` or appear in the graph registry. If it ever
does, `test_portability.py` will start asserting purity rules against something
that cannot satisfy them, and the honest answer will be to weaken the test.

It shipped as `harness/epic.py`, and `run_epic` is the whole of it.

The parts of this that *are* model calls — `validate_chunk`, `validate_phase` —
are nodes, and nodes belong to graphs. They shipped as
`graphs/delivery/phase_validate.py`, the `phase-validate` graph, which the driver
invokes exactly the way it invokes `lifecycle-propose`. See *Decision 3*.

## Roles and kinds this needs

| Name | Kind | Status | Notes |
|---|---|---|---|
| `validate_chunk` | role | **shipped, optional** | did this task actually satisfy its own description |
| `validate_phase` | role | **shipped, optional** | do these tasks, together, accomplish the phase |
| `stack_rebase` | write kind | **shipped** | rewriting a branch other work is stacked on |

All three landed in the base cartridge. The two roles are additions to
`roles.optional`, which is ordinary. `stack_rebase` is an addition to
`write_kinds`, also ordinary. What was *not* ordinary is discussed under
*Comfort levels*, below, and it is the finding that most affected this design.

**Args:** `run_id`, `date`, `cartridge` (resolved, required, no fallback),
`initiative` (the work store's own output, read by the CLI and passed in — a
driver that read the store itself would put the filesystem back inside the thing
under test), `repo` (required: stacking is real branches in a real repository,
and there is no honest way to fake that), `max_parallel`, and the harness's usual
`ledger_path` / `runs_dir` / `worktree_root` / `provider_profile` / `assume`.

There is no `comfort` argument. A comfort level is a cartridge — `local-comfort0`
and `local-comfort1` in `agent-cartridges` — and a flag that could override the
resolved ramps would be a second source of truth about what a team has earned.

**Returns:** `{run_id, date, initiative, phases[], tasks[], quarantined[],
proposals[], totals}` — where `totals` reports `phases_complete`,
`phases_partial`, `tasks_quarantined`, and `stacks_rebased`.

## The central claim: producing is not landing

An unattended swarm and an autonomy policy that gates every write look like they
cannot both be true. They can, because they are about different acts.

**The swarm runs to completion producing draft PRs.** A draft PR has no effect
until a human opens it — that is why `draft_pr_create` carries `risk: low` in the
base taxonomy while `merge_main` carries `never`. Producing work is cheap to undo:
the artifact is a branch and a draft, and the cost of a wrong one is a branch
nobody reads. Landing work is not, and landing is where the policy lives.

So the terminal state of an unsupervised run is a stack of draft PRs across every
phase, one per task, with nothing merged, nothing marked ready, and no row in any
system of record that a human did not approve. The swarm never *needs* autonomy
to finish. It needs autonomy only to save you clicks afterwards, which is exactly
the thing that should have to be earned.

This is the whole argument for why the swarm may default to running unattended.
If a reviewer of this spec disagrees with one paragraph, it should be this one.

## Stacking, and the blast radius of a rebase

Tasks within a phase are independent **by construction** — that independence is
what `initiative-decompose`'s adversary exists to establish, and it is what makes
them parallelisable at all. So every task in a phase branches from the same base
and its draft PR merges independently. Tasks do not stack on each other.

Only **phase-to-phase** relationships stack. A phase whose dependencies are
satisfied takes its base ref from the parent phase's branch head rather than from
the default branch. Phases with no parent branch from the default branch.

This is a deliberate constraint, not a simplification. If tasks stacked on tasks,
a change to any one of them would force a rebase of everything downstream inside
the phase — N rebases, N possible conflicts, unattended and concurrent, which is
how stacked-PR workflows already fail for humans. Restricting stacks to phase
boundaries bounds the blast radius to exactly the places a human is most likely
to be looking anyway.

**A rebase is a write, so it is a kind.** `stack_rebase` rewrites a branch that
other work depends on; it can silently discard a commit, and it is not obviously
reversible from the outside. Governing it through the same ramp as everything
else costs nothing and buys the ability to say "rebase automatically, but only
once you have done fifty of them cleanly."

## Two validators, and the failure the second one exists for

`validate_chunk` asks whether one task satisfies its own description. It is
cheap, it runs per task, and it is largely a restatement of the `done_criteria`
the cartridge already carries.

`validate_phase` is the one that earns its place. It reads **the phase's original
goal** — not the task list, the goal — and asks whether what now exists
accomplishes it. Five tasks that each went green and do not add up is a real
failure mode, and it is invisible to every check below it: each task passed its
own review, each draft PR is defensible, and the phase is nonetheless not done. A
`validate_phase` that only confirms every task finished is ceremony, and should
be left unbound instead.

Both are optional roles. A team that binds neither gets a swarm that reports
task completion and makes no claim about phase completion, which is honest.
Binding either one to a human is how a comfort level becomes a gate.

## Comfort levels — and what is actually expressible

The intent was that comfort levels are named bundles of existing `write_kinds`
ramps plus `caps` — presets, not new machinery. **That is true for two of the
four levels and false for the other two**, and the reason is worth stating
plainly because it is a governance question, not a coding one.

`cartridges/base/cartridge.yaml` says: *"A team may TIGHTEN a risk or ramp. A
team may never loosen one."* Against the shipped taxonomy:

| Level | Intent | Expressible as a preset? |
|---|---|---|
| 0 | everything proposes | **Yes.** Tighten every relevant kind to `gated`. Ships as `local-comfort0` |
| 1 | draft PRs auto-created | **Yes, once earned.** `draft_pr_create` is already `ramp: eligible`; it auto-applies after `graduation_n` clean outcomes, not on being switched on. Ships as `local-comfort1` |
| 2 | graduated kinds may mark ready | **No, still.** `pr_ready_flip` is `ramp: gated` in base. A team cannot loosen it, and nothing here justifies loosening it for everyone. Unshipped |
| 3 | graduated kinds merge within a phase | **Yes, now** — by narrowing the kind rather than loosening a ramp. See below |

Level 3 changed, and the way it changed is the point. The old row said `merge` is
`ramp: never` and a team cannot loosen it, which was true and was the wrong
question. One `merge` kind priced two unlike acts identically: joining a task
branch to the phase branch above it, and putting code on the branch everyone else
builds from. Only the second is irreversible in the way that argues for a
permanent human. So `merge` split into `merge_stack` (`eligible`) and `merge_main`
(`never`), and *within-initiative* stack merges became expressible without anyone
loosening anything — `merge_main` inherited the old kind's posture, and
`merge_stack` is a new, narrower act that never had a kind of its own.

A team that does not want stack merges earnable tightens `merge_stack` to `gated`
in its own cartridge, which is exactly what the comfort-1 bundle does. The
one-way tighten rule is what makes that toggle legal, and it is why the split is
not a loosening dressed up as a refactor.

Level 1 carries a subtlety that reads as a bug if it surprises you: a preset
cannot *grant* autonomy, only permit it to be earned. `autonomy_policy` returns
`AUTO` for an `eligible` kind only after the streak clears the bar, and
`graduation_n` lives in the cartridge's `policy` block, globally, not per kind.
Setting `graduation_n: 0` to make level 1 immediate would make *every* eligible
kind immediate. There is no per-kind graduation override, and adding one would be
a change to the trust model rather than a preset.

Level 2 still requires editing the base taxonomy — loosening `pr_ready_flip` for
everyone — which the base forbids teams from doing and which nothing here
justifies doing on their behalf. **Levels 0 and 1 shipped as bundles; level 3
became expressible through the split; level 2 remains unshipped.** The swarm's
value never depended on 2: clicking "ready" on work you were going to read anyway
is not the expensive part of the job.

## When a task fails

`on_task_failure` is a cartridge setting: `continue_independent` (default),
`halt_phase`, or `quarantine`.

The default is not a new behaviour — `harness/phase.py` already collects a failed
task into `failures` and lets the rest of the phase finish, on the stated grounds
that "one task failing must not take the phase with it." This spec names that
policy and adds the ability to override it.

Halting discards the parallelism the decomposition just bought, so it is the
wrong default; it exists for phases where a partial result is worse than none.
`quarantine` sets the failed task aside with its diagnosis and continues, which
is `continue_independent` plus a place to look afterwards.

A phase can therefore end **partially complete**, and `validate_phase` must have
an opinion about that rather than treating a quarantined task as absent. A phase
that is 4-of-5 done is a different fact from a phase that is done.

```mermaid
flowchart TB
    INIT["initiative, from initiative-decompose<br/>phases, tasks, dependency edges"]
    INIT --> SELECT

    subgraph DRIVER["the swarm driver: an I/O edge, not a graph"]
        SELECT{"phase ready?<br/>every dependency phase validated"}
        BASE["base ref: parent phase branch,<br/>or the default branch when unparented"]
        FANOUT["run_phase: lifecycle-propose per unblocked task,<br/>each in its own worktree"]
        VCHUNK["validate_chunk<br/>role, once per task"]
        VPHASE["validate_phase<br/>role, reads the phase goal"]
        QUAR["quarantine<br/>per on_task_failure"]
    end

    SELECT -- "blocked" --> WAIT(["wait for a dependency"])
    SELECT -- "ready" --> BASE
    BASE --> FANOUT
    FANOUT -- "one draft PR per task" --> VCHUNK
    VCHUNK -- "task failed" --> QUAR
    VCHUNK -- "task satisfied" --> VPHASE
    QUAR --> VPHASE
    VPHASE -- "goal unmet, or partial" --> SELECT
    VPHASE -- "goal met" --> DONE["unblock dependent phases"]
    DONE --> SELECT

    FANOUT -. "proposals" .-> POLICY{"autonomy_policy<br/>has this kind graduated?"}
    POLICY -- "propose" --> GATE{{"human gate"}}
    POLICY -- "auto, graduated kinds only" --> ARM["apply arm, a role"]

    ARM -. "never reaches" .-> MERGE["merge_main"]

    style MERGE stroke-dasharray: 5 5
```

The dashed edge carries the same weight here as it does in `lifecycle-propose`,
and the split is what made it precise rather than sweeping: no path through this
driver reaches the default branch, at any comfort level, on any streak. Stack
merges inside an initiative do happen, once `merge_stack` has been decided or
earned. The swarm's output is branches and drafts, and the branch everyone else
builds from is not one of them.

## Prerequisite: nested invocation

This needed one primitive the harness did not have — **a graph invoking graphs
and waiting on the results**, with the invoked run's proposals flowing into the
same policy, gate and ledger as everything else rather than into a side channel.
It shipped as `harness/invoke.py`, and `run_phase` is now a thin wrapper over it.

It is shared with two other pieces of deferred work: the bounded fix loop in
`lifecycle-propose`, and the chief-of-staff dispatcher. Building it once was the
point. What this spec needed from it, specifically:

- invoke a named graph from the registry with constructed args
- bound concurrency, which `run_phase` already does
- collect proposals across invocations under **one** `run_id`, so the manifest
  hashes one cartridge and `_require_single_scope` stays satisfiable
- surface a child's `ContractViolation` as a task failure, not a swarm failure

All four hold. The driver invokes `lifecycle` and `validate` by registry name,
bounds concurrency at `max_parallel`, records one manifest per phase under
`run_id:phase` so the manifest hashes one cartridge, and takes a child's
`ContractViolation` or `RunnerError` as a quarantined task.

## Decisions — 2026-09-01

The three open questions, closed.

**1. Are phase boundaries ever ungated?** *Decided: they are gated by
`merge_stack`'s ramp, which is the same answer as "no" for every team that has
not measured one.* A phase boundary is the last place a human can redirect the
work cheaply — after it, the next phase's tasks branch from this phase's head and
a change means rebasing them all. Since the split, that boundary has a kind of its
own: `merge_stack` is `eligible`, so it gates until it is earned and can then be
earned through the ordinary ramp, and a team that wants it permanently human pins
it `gated` under the one-way tighten rule. The comfort-1 bundle does exactly that.
`merge_main` stays `never`, at every comfort level and on every streak, and no
path through this driver emits or executes it.

The driver makes that gate load-bearing rather than decorative: **a phase is
complete only when its merges actually happened.** A met goal on work that never
reached the phase branch is a phase whose dependents would branch from nothing.

**2. Can a partially-complete phase unblock its dependents?** *Decided: blanket
no.* A phase unblocks its dependents only when `validate_phase` says the goal is
met. Blanket yes turns one quarantined task into a phase of work built on ground
that is not there. The refinement — unblock when the quarantined tasks are not on
the dependent path — asks `validate_phase` to reason about *which* task failed
rather than how many, which is a heavier ask of that role than anything else here.
So the validator *reports* it, as `quarantine_blocks_dependents`, and nothing acts
on it yet. The field exists so the judgment can be measured before it is trusted,
which is the same shape as every other ramp in this system.

**3. Where do the validators live?** *Decided: in a graph.* `phase-validate` is a
two-node graph — `validate_chunk` per task, `validate_phase` once — and the driver
invokes it through `invoke_graphs` like anything else. The ceremony buys the thing
that matters: the nodes stay under the portability suite, the cartridge is
required with no fallback, and the run is replayable. A driver calling the runner
directly would have put the two judgments the phase boundary rests on outside
every rule the rest of the system is held to.

## What the implementation deviates on

Four places where the shipped driver is narrower or differently shaped than the
text above, each recorded rather than quietly absorbed.

**Validators run between the fan-out and the merges.** The phase verdict judges
the union of what the tasks produced, on their own branches, before any of it is
joined to the phase branch. It is not a re-read of the merged branch. The verdict
is therefore about work that exists and is checked, not about a state the driver
already committed to — and a phase that does not add up is caught while nothing
has moved.

**A v1 stack supports one parent phase.** A phase with two parent phases is not
representable as a single git stack: one stack has one base ref. Such a phase is
marked blocked with that diagnosis and does not run, rather than the driver
picking a parent and silently building on half the ground.

**Per-phase manifests, under `run_id:phase`.** One phase, one cartridge, one
scope, so `_require_single_scope` stays satisfiable and a phase's ledger rows are
attributable to the phase that earned them.

**A draft is a local branch until a forge arm exists.** After the gate, the driver
creates `epic/<initiative>/<phase>--<task>` from the scratch branch the work was
built on. The `--` is not cosmetic: git cannot hold both `refs/heads/epic/i/p1`
and `refs/heads/epic/i/p1/t1`, because one is a file where the other needs a
directory. Nothing is pushed and nothing is opened, which is the same blast radius
the taxonomy prices `draft_pr_create` at.

One more, smaller: the work store's phases are bare directory names, so a phase
goal is reconstructed from the phase name and the initiative's own prose when the
store carries nothing better. Decompose-produced initiatives should eventually
land per-phase goals in the store; `validate_phase` is only as good as the goal it
is handed, and a reconstructed one is the weakest input in this design.

**Deferred from v0:** the bounded fix loop (a task that fails validation is
quarantined, not retried), cross-initiative scheduling, cost and token budgets
across a whole swarm (`policy.budgets` bounds a run, not a swarm), acting on
`quarantine_blocks_dependents`, and comfort level 2.

**Status:** implemented. `harness/epic.py` is the driver — no `SPEC`, not in the
registry, and it must stay that way. `graphs/delivery/phase_validate.py` is the
`phase-validate` graph it invokes for the two judgments. `validate_chunk`,
`validate_phase`, `merge_stack` and `stack_rebase` all exist in the base
cartridge, so this resolves.
