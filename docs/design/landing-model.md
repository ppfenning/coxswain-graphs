# The landing model

Status: approved by the chair 2026-09-08 on Pat's direction (2026-09-08): "PRs for tickets within the same
phase should merge with each other with one parent PR. The tickets get stacked in the phase PR, and the PR is
diffed to main. Once phase is complete, we only have one merge to main"; and "phase PRs should go through a
check to remove verbose changes, we want what goes into main to have all the fat trimmed." Groups intake G2
from `workspace/plans/agent-platform/2026-09-08-intake-grouping.md`. Committed identically to
`coxswain-tools`, `coxswain-graphs` and `coxswain-cartridges` under `docs/design/` so any build seat can read it.

## 0. The defect, once

The epic builds a PHASE: every task branch is merged `--no-ff` into `epic/<initiative>/<phase>`
(`harness/epic.py:1303`), and `validate_phase` judges the tasks together (`:997`, `phase_verdict`). But
`cox runs land` lands a TASK: it cherry-picks one commit onto `pr/<task>` from main (`agent_tools/land.py:69
land_plan`), squash-merges it, then (a) deletes every `agents/<run>/*` branch — the siblings included — and
(b) keeps the phase branch, which the next epic run refuses as stale (`epic.py:684-700`). Measured
2026-09-07/08: ~10 stale-branch refusals, 2 manual git recoveries, 4 lint-only CI failures after the PR was
already open, one `cox runs land` failure from a relative `runs/` path. Every one disappears when the unit
that is built is the unit that lands.

> The phase is the unit of landing. One PR per phase, diffed to main, its tickets stacked inside it, its
> `validate_phase` reasoning as the PR body, one squash to main, then every branch the phase owned is gone.

## 1. `cox runs land <run> --phase <phase>`  (tools)

New mode beside `--task`; the default when the run record shows more than one task in the phase.

- **Precondition, pure:** `phase_landable(items, records) -> str | None` — every work item in the phase is
  `done` or `dropped` (§3), and every `done` item has a task record whose `_approved` (`land.py:41`) is None.
  Returns the first reason it is not; the edge prints it and exits 2.
- **Plan** (`land_plan` gains a `phase` branch of logic; same step vocabulary, `cli.py:272-310` executes it):
  `pick_branch epic/<initiative>/<phase>` → `checks` (§4, run in a fresh worktree of that branch) → `push`
  → `pr_create` with title `epic <initiative>: <phase>` and body from `phase_pr_body` (§2) → `wait_checks` →
  `merge --squash --delete-branch` → `clean_phase` → `mark_done` for every task record in the phase.
- **`clean_phase`** deletes `epic/<initiative>/<phase>`, `epic/<initiative>/<phase>--*` and
  `agents/<run>/<task>` for exactly the tasks in this phase, plus their worktrees under the configured root.
  Nothing else. It is the ONLY step that deletes branches; the existing run-scoped `clean` is removed.
- **`--task` survives** for a one-task phase and for emergencies, with its cleanup narrowed to
  `agents/<run>/<task>` and its own phase branch left for `--phase` to finish. Its "exactly one commit ahead"
  rule is unchanged.
- **Paths:** `_land_record` (`cli.py:234`) resolves `runs/` against the profile's `workspace_dir` and
  accepts `--runs-dir`; its refusal names the absolute directory searched and the count found:
  `land: looked in <abs>, found N task records, expected 1`.
- The stacked-task rule from intake 2026-09-06 ("a stacked task lands into its parent's PR") is SUBSUMED:
  inside a phase the stack is already merged on the phase branch.

## 2. The PR body carries the phase's evidence  (tools)

`phase_pr_body(phase_record, task_records) -> str`, pure: the phase goal line; `phase_verdict.reasoning` from
the phase record (`runs/<run>:<phase>.json`, key `phase_verdict`); then one block per ticket — id, title,
review verdict, adversary verdict, arbitration verdict (or "unanimous"), fix-loop attempts, files touched.
Dropped tickets are listed under "Dropped" with the reason from their body. No prose is generated; every line
is a field the record already holds.

## 3. `dropped` is a state  (cartridges, graphs)

- `core/workstore.py:61 STATES` gains `"dropped"`; `set_state` accepts it; `read_initiative` treats it as
  terminal like `done`. An item is dropped by a person or the chair, never by a graph.
- `harness/epic.py` skips `dropped` tasks when selecting work and when computing phase completeness
  (`record["status"] == "complete"` at `:581` counts done+dropped as complete). `agent_tools/route.py`
  `initiative_states` / `initiative_summaries` do the same, so `cox route context` reports a phase with one
  dropped ticket as complete, not stuck.
- The six items today marked `done` with a "superseded" note (intake 2026-09-08 "no state for a superseded
  ticket") are moved to `dropped` by the chair once the loader accepts it; not this spec's code.

## 4. The checks the project runs are the checks the harness runs  (cartridges, tools)

- `cartridges/local/cartridge.yaml:84` `checks:` becomes `[{name: lint, cmd: "ruff check ."}, {name: tests,
  cmd: "pytest -q"}]`, lint first because it is cheapest. `harness/checks.py run_checks` already runs the
  list and records `unrunnable`; resolution follows the pytest precedent (venv first, `uv run`, then PATH).
- `land.checks_argv` returns a LIST of argvs, one per configured check, built from the same `checks:` the
  cartridge resolves for the repo — the edge passes the resolved list in `repo_facts["checks"]`; the
  `checks` step runs them in order and stops at the first failure, naming it. A land never opens a PR that
  CI will fail for a rule the worktree could have run.

## 5. Trim the assembled diff, under a coverage floor  (graphs, last)

After `validate_phase` returns satisfied and before the phase is marked complete, the epic runs `style_pass`
ONCE over the assembled phase diff (`git diff main...epic/<initiative>/<phase>`) with the brief it already
has ("make the narrow style edit an approved diff still needs, or return nothing") plus one sentence: "remove
duplication the tickets introduced separately; remove shims whose exit condition the phase has met". Its
patch is applied on the phase branch only if the mechanical floor holds:

    pytest --collect-only -q  before → set A ;  after → set B ;  require A ⊆ B

and the configured checks pass. Line count may fall; collected test ids may not. A trim that trips the floor
is discarded and recorded on the phase record as `trim: refused (coverage floor)`, never retried. This is
`stack_rebase`-class write on a branch the phase owns; it stays inside the run and is visible in the PR body
as `Trim: <n> lines removed` or `Trim: none`.

## 6. Out of bounds

No change to review, arbitration or `validate_phase` judgement; no auto-merge policy change (`merge_main`
stays `never` in the base cartridge — the chair runs `land`); no rewriting of history on `main`; no change to
how task branches merge into the phase branch. The `pr/<task>` naming stays for `--task` mode.

## 7. Tests each part must carry

§1 `phase_landable` on a literal phase with (all done), (one ready), (one dropped + rest done), (one done but
unapproved); `land_plan` in phase mode yields the step list above and `clean_phase` names only this phase's
branches. §2 `phase_pr_body` on a two-ticket literal record with one arbitration and one unanimous. §3
`set_state(path, "dropped")` round-trips; a phase of done+dropped is complete; `initiative_summaries` reports
it complete. §4 `checks_argv` yields two argvs from a two-check config and the `checks` step stops at the
first failure. §5 the floor accepts A ⊆ B and refuses a removed test id; a refused trim leaves the branch
untouched.
