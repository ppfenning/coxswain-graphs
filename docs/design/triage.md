# Triage and earned autonomy

Status: approved by the chair 2026-09-15 under Pat's standing order (2026-09-08) and his direction of
2026-09-08: "should fails pass through a new graph to get the work back into a queue? rather than you just
picking up the fix?" and "removing human gates in places like that is the exact goal". Groups intake G7
from `workspace/plans/agent-platform/2026-09-08-intake-grouping.md`:
`a-triage-graph-should-classify-quarantines-and-re-queue`,
`provider-profile-edits-do-not-reset-autonomy-streaks`, plus the 2026-09-09 item
`autonomy-streaks-are-not-scoped-to-the-model-binding`. Committed identically to `coxswain-graphs` and
`coxswain-cartridges` under `docs/design/`. Depends on G1 (the observed record) — landed.

## 0. The defect, once

When the attempt cap fires, "a person decides": the chair reads the trace, classifies the failure, edits
the ticket and relaunches — five times on 2026-09-07/08, twice more on 09-09. Steps two and three are
judgment; everything around them already has a mechanism. Separately, approved work is DROPPED without
any quarantine (a sibling task's plan node dies, the run never reaches its bookkeeping) and only a person
looking finds it. And the autonomy streaks that would let such a graph earn its way past the gate are
scoped to a provider profile FILENAME, not its content, and not to the model a node was bound to — so a
track record earned on one model silently transfers to another.

> A quarantine at the cap is classified by a graph from facts the harness computed, an amendment may
> specify and never relax, dropped approved work is found by a scan and landed rather than rebuilt, and a
> streak belongs to the exact configuration that earned it.

## 1. The `triage` graph  (graphs)

`graphs/ops/triage_quarantine.py` (`triage-propose` is alert triage and stays as it is), registered in
`harness/cos.py _KNOWN_GRAPHS` and `harness/cli.py`; launched by the epic when a task's attempt cap fires
(`core.workstore` attempts >= 2 against the current spec) and by hand via `cox route launch triage
--initiative <dir> --task <id>`. Copies `retro-propose`'s discipline exactly: the model cites, the graph
substantiates.

- `facts` (no model): from the work item and the task records of its attempts — quarantine reason and
  `kind` per attempt, review / adversary / arbitration verdicts, `fix_loop.stopped`, the trace's final
  result `subtype` per node, the evidence array, and whether the attempts predate the ticket's last
  revision (`attempts[].ts` vs the item file's last commit). Keyed facts: `attempt-<n>|<field>`.
- `triage` (role `triage`, deep tier, ceiling `role_budget_usd.triage` default $0.80): returns
  `{class, diagnosis, cites[], action}` with `class` one of `ticket_defect | platform_defect | shape |
  genuine_reject`. A cite naming a fact key not in `facts` is a fabricated citation and the answer is
  refused; zero cites is refused; a `diagnosis` that quotes no objection text from the record is refused.
- `emit`: by class —
  `ticket_defect` -> a `ticket_amend` proposal (§2) carrying the exact text to append;
  `platform_defect` -> an `item_create` intake proposal (existing kind) naming the mechanism;
  `shape` -> a `decompose` handoff (the split is decompose's job, not triage's);
  `genuine_reject` -> `notify` (gated, no ramp) and the item stays `ready` with the classification recorded.
- Recorded on the work item under `attempts[].triage: {class, diagnosis, run}`; the cap is cleared by
  the arm only on `ticket_defect`/`shape`/`platform_defect`, and NEVER twice with the same `(class,
  diagnosis)` on one item — that pair escalates to a person with both records quoted.

## 2. `ticket_amend`: specify, never relax  (cartridges, graphs)

- `cartridges/base/cartridge.yaml` write kinds: `ticket_amend: {risk: low, ramp: eligible}`.
- The arm (`workstore-item`) applies an amendment by APPENDING a dated section to the item body. The
  harness checks the resulting diff mechanically before proposing: purely additive (only `+` lines,
  frontmatter untouched except `attempts`) -> the proposal may ride the kind's streak; any `-` line or any
  frontmatter change -> `ramp` is forced to `gated` for that proposal whatever the streak. No model decides
  this; `difflib` does.
- The amendment text must quote the objection it answers (a line from the record) and state how the
  addition satisfies it; the `emit` node refuses text without a quote.

## 3. Dropped approved work is found by a scan  (tools, graphs)

`cox runs stranded [--json]` (tools): for every run directory, each task record under
`runs/<run>/tasks/<phase>/<task>.json` whose `review` and `arbitration` verdicts approve while the work
item is not `done` prints `run, task, branch, remedy` where remedy is the exact `cox runs recover` /
`cox runs land --task` line. No model. `assemble_docket` (`harness/cos.py`) carries the count as
`stranded` so the coxswain can dispatch the land instead of a rebuild. Refused work (`revise` verdicts)
never appears — only approved work may be recovered by landing.

## 4. A streak belongs to its configuration  (graphs, cartridges)

- `provider_profile` on every ledger row and in the streak filter becomes the sha256 of the RESOLVED
  profile file's bytes, prefixed by its stem: `claude-code@<12 hex>`. `harness/cli.py` (both sites that
  use `Path(args.provider_profile).stem`) computes it; `split_by_policy` compares it. The profile's
  header comment then tells the truth. Historical rows with a bare stem form their own scope and simply
  stop counting — no rewrite.
- Ledger rows gain an optional `model` (the binding the proposing node ran under). `core/policy.py`
  `SCOPE_KEYS` gains `model`; rows lacking it are scoped as `model: None`. The graph writing a row knows its
  binding from the runner's call record. `_require_single_scope` still raises on a mixed set, so the
  caller filters on the proposal's own binding.

## 5. Out of bounds

Auto-applying `genuine_reject`; letting triage edit frontmatter other than `attempts`; changing the cap
(2) itself; alert triage (`triage-propose`); the router (P4).

## 6. Rules, one literal test each

1. `facts` keys every attempt's reason, kind, verdicts, stopped and subtype; a cite outside the key set is
   refused as fabricated; zero cites is refused.
2. A `ticket_defect` answer whose diagnosis quotes no record text is refused.
3. The same `(class, diagnosis)` clearing a cap twice on one item escalates instead of clearing.
4. An additive amendment diff keeps `ramp: eligible`; one `-` line forces `gated`.
5. `cox runs stranded` lists an approve/approve record with a `ready` item and omits a `revise` one.
6. `provider_profile` is `stem@sha12` of the file bytes; editing one byte changes the scope.
7. A row without `model` never counts toward a streak read for a binding.
