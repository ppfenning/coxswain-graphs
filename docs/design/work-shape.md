# Work shape

Status: approved by the chair 2026-09-08 on Pat's direction (2026-09-08): "the coxswain will make the
distinction of mechanical vs novel. not everything can work through decomposition"; "this needs to be a
formal path with coxswain"; "can the chair (or a dedicated agent) attempt to group intake tickets into a
single spec if they prove to be similar?". Groups intake G3 from
`workspace/plans/agent-platform/2026-09-08-intake-grouping.md`, plus the worktree-leak item. Committed
identically to `coxswain-graphs`, `coxswain-tools` and `coxswain-cartridges` under `docs/design/`.

## 0. The defect, once

The platform has one shape for work — decompose into a phased DAG and build each ticket in its own seat —
and decides that shape nowhere: the chair launches `decompose` because it is the only graph that takes an
idea. Measured 2026-09-07/08: the leader→chair rename cost $53 across 22 runs and 260 model calls for a
transformation `grep` and `sed` perform, because every per-file ticket re-derived the same five decisions and
no ticket could see the whole. Meanwhile tickets routinely name paths and commands the build seat cannot
reach (three instances in one night, each found only after a run), two tickets sharing a test file are
handed to two seats, and related intake items are decomposed separately when one change closes all of them.

> Shape is decided once, by the coxswain, before anything is decomposed; a mechanical sweep is one judgment
> call, one application, one deterministic verification; and a ticket is linted against the seat's actual
> reach — statically, with no model — before it is dispatched.

## 1. The `sweep` graph  (graphs)

A new graph beside `decompose`, for work that is ONE RULE APPLIED IN MANY PLACES.

Nodes, in order:
- `sweep_plan` (role `plan`, deep tier, its own ceiling `role_budget_usd.sweep_plan`, default $1.50): reads
  the whole repository (Read/Grep/Glob) and writes the MAP — old→new pairs or the rule as a function of the
  match; the EXCEPTIONS with a reason each (shims kept, shipped notes untouched, compatibility surfaces); the
  APPLICATION as either a shell script over `grep`/`sed`/`git mv` or, only where a rule cannot be written, one
  build brief with the whole map in it; and the POSTCONDITION as a list of `grep` patterns that must return
  zero matches plus the configured checks. Output schema: `{map, exceptions, application: {kind: script |
  build, body}, postcondition: [pattern]}`.
- `sweep_apply`: the harness runs the script in the phase worktree (no model), or runs ONE `build` call with
  the brief and the full map (role `build`, ceiling `role_budget_usd.sweep_build`, default $2.00).
- `sweep_verify` (no model): every postcondition pattern greps to zero over the worktree, excluding the
  exceptions' files; the configured checks pass. A failure names the pattern and the first three matches
  and the graph stops; nothing is retried by a model.
- Then the ordinary review pair and arbitration over the ONE resulting diff, and the phase lands as one PR
  (`docs/design/landing-model.md` §1).

Registered in `harness/cos.py:72 _KNOWN_GRAPHS`, `harness/cli.py` graph choices, and `cox route launch
sweep --idea <file> --initiative-id <id>` in tools (`agent_tools/cli.py:1885` siblings). Recorded like any run:
`runs/<run>.json`, `usage.json`, `calls.jsonl`.

## 2. The coxswain decides the shape  (graphs, cartridges)

`graphs/ops/coxswain.py` `DISPATCH_SCHEMA` selections gain an optional `shape: "sweep" | "decompose"` and a
required `why` that, for `sweep`, names what made it mechanical: one rule, many places, no unit needing its
own design. `harness/cos.py assemble_docket` marks `sweep` runnable when an idea is queued. The
`dispatch-graphs` skill (`coxswain-cartridges/skills-plugins/local-skills/skills/dispatch-graphs/SKILL.md`)
gains a **Shape** discipline paragraph: decompose only what needs design per unit; a rename, a path move, a
signature change across callers, a doc sync, is a sweep; when in doubt, sweep first and decompose what the
sweep's exceptions list leaves behind. The chair follows the same rule when launching by hand, and records
the shape decision in the initiative body until the coxswain is the default launcher.

## 3. Static ticket lint before dispatch  (graphs, tools)

Pure `lint_tickets(tasks, tree, grants, repo) -> list[Problem]` over a decomposed DAG, with no model, run at
the end of `graphs/delivery/initiative_decompose.py run` (after `_local_cycle`) and again on any amendment
that changes a ticket body. `Problem(task, rule, detail, fix)`. Rules:

- **reach:** any absolute path, `workspace/`-rooted or `~/`-rooted path in body or surfaces that is not inside
  the target repository → `fix: move the artifact into the repository or drop the reference`.
- **grant:** any command named in the body's evidence or fence that the role's `tools` grant and the sandbox
  allowlist do not permit (`cox`, `uv`, `gh`, `git push`, `ruff` when not installed) → `fix: name only
  pytest, git status, git diff`.
- **coupling:** two tickets in one phase whose `surfaces` share a test file, or whose named modules import
  one another → `fix: merge, or order with needs`.
- **size:** a body over ~700 words → `fix: point at a spec file in the repository` (the measured `plan`
  death band).

Decompose REFUSES a DAG with a `reach` or `coupling` problem and returns the corrections to the
`work_item_arm` (the graph already has `_apply_corrections`); `grant` and `size` are recorded as warnings on
the ticket (`lint:` list in frontmatter) and printed. `cox route lint <initiative>` in tools runs the same
pure function over a filed initiative so the chair can check a hand-written ticket before launch.

## 4. `consolidate`  (cartridges, graphs)

A new write kind `consolidate: {risk: low, ramp: deferred}` in the base cartridge: emit ONE merged ticket
and mark the superseded ones `dropped` (`landing-model.md` §3). Triggered only by a finding class, never by a
model's preference: a review, adversary or validate finding stating that a build cannot satisfy its ticket
without touching a file another ticket in the phase owns. The epic proposes it through the ordinary gate
with both ticket ids and the finding quoted. Run 16 of `tools-chair-rename` produced this artifact
unprompted; this names it.

## 5. Group intake before decompose  (graphs, tools)

A `group_intake` node (optional role, standard tier) in the coxswain graph, run when the docket shows two or
more un-decomposed intake items. Input: the items' titles and bodies. Output: `groups: [{subject, items:
[id], spec_hint}]`, where every group cites the items it joins and names the ONE change that closes them;
a group of one and a queue with no groups are legitimate answers. Bounds, both mechanical: a group may not
exceed 5 items or 1,500 words of source, and two items join only on a shared CAUSE stated in the
`subject`, not a shared keyword. The harness writes `plans/intake-groups/<date>.md` (one file, all groups)
into the workspace via the arm, and the chair turns a group into a spec plus a decompose intake exactly as
G1 and G2 were done by hand. `cox route groups` prints the latest file.

## 6. A run cleans up on every exit  (graphs)

`harness/epic.py` and `harness/cli.py` remove the run's worktrees (`<worktree_root>/<run_id>/**`) and
prune dead registrations in a `finally` on every exit path — quarantine, budget stop, `RunnerError`,
`KeyboardInterrupt` — not only on land. Branches are left for `land`. A run that wants its worktree kept for
post-mortem sets `--keep-worktrees`, which moves them under `<worktree_root>/_kept/<run_id>` with a 7-day
expiry the next run sweeps. Before creating a phase worktree, `_open_phase_worktree` runs `git worktree
prune` so a stale registration can never block a branch name (the 2026-09-08 `tools-chair-rename-18`
failure).

## 7. Out of bounds

No change to review or validation judgement; no auto-launch of `sweep` without the coxswain or the chair
naming the shape; no merging of tickets by a model outside the `consolidate` kind; no change to the
`epic_threshold` sizing in `scope-work` (magnitude stays there; shape lives in the coxswain).

## 8. Tests each part must carry

§1 a sweep over a literal three-file tree with one exception applies the script, verifies the postcondition,
and refuses when a pattern still matches (naming the match). §2 a selection with `shape: sweep` and no
`why` is refused; the docket marks `sweep` runnable only with an idea queued. §3 one literal DAG per rule:
a `~/` path, a `cox` command, two tickets sharing a test file, a 750-word body; a clean DAG yields `[]`. §4
a `consolidate` proposal carries both ids and the quoted finding. §5 two items with one stated cause group;
two items sharing only a keyword do not; a six-item group is split. §6 a run that raises after creating a
worktree leaves no directory and no registration.
