# Cost bounds

Status: approved by the chair 2026-09-15 under Pat's standing order (2026-09-08). Answers Pat's questions
of 2026-09-08 ("should cost bounds be variable? ... should we have a leveling system for cost like
strict/moderate/liberal?"). Groups intake G5 from
`workspace/plans/agent-platform/2026-09-08-intake-grouping.md`:
`derive-cost-bounds-from-measured-distributions`, `separate-operator-spend-caps-from-provider-shape`,
`output-not-input-dominates-spend`. Committed identically to `coxswain-cartridges`, `coxswain-tools` and
`coxswain-graphs` under `docs/design/`.

## 0. The defect, once

One number means two things. `budget_usd` in the provider profile is a SHAPE ceiling ("a correctly sized
task on this model finishes under this"); the operator's wallet has no home, so it leaks into the same
number and erodes it. A stop therefore means nothing definite: on 2026-09-07/08 three identical
`error_max_budget_usd` lines needed three different responses (raise an unmeasured ceiling; fence a
mis-shaped task; raise a censored ceiling), and only the trace told them apart. The ceilings themselves
were set by hand from distributions computed by hand, and the `plan` distribution was censored $0.005
under its own cap. Meanwhile `ceiling_usd` is plumbed through `usage_window`/`pacing` and never wired, so
every session prints "window is unmeasured".

> Shape ceilings are declared in the profile and EARNED from the stats store; operator spend caps live on
> the machine; a stop names which of the two it hit; and a level says what that stop means.

## 1. Two axes, two errors  (tools, graphs)

- **Operator spend** lives in `~/.config/agent-tools/profile.yaml` (machine-local, unversioned):
  ```yaml
  spend:
    window_ceiling_usd: 300     # the 5-hour window the operator will tolerate
    node_cap_usd: 2.00          # no single node session past this, whatever its shape ceiling
  ```
  Both optional. `agent_tools/cli.py` (`cox route context`, ~line 1923) and `agent_tools/home_screen.py`
  (~line 54) pass `ceiling_usd=spend.window_ceiling_usd` to `usage_window.gather`, so `pacing.assess`
  reports the measured window and its stop rule instead of "unmeasured".
- `cox route launch *` passes `--node-cap-usd <node_cap>` to the harness when set. `harness/cli.py`
  accepts it; the runner's effective per-node limit is `min(shape_ceiling, node_cap)`.
- A stop at the shape ceiling stays `error_max_budget_usd` ("split the task"). A stop at the node cap is
  `error_spend_cap` ("the operator's limit"), recorded on the task record and in the quarantine reason
  with both numbers. The two are never conflated in a message.

## 2. Bounds earned from the store  (tools)

`cox stats bounds [--json] [--level strict|moderate|liberal]`, reading `stats.db`, per `(role, model)`:
`n`, `p50`, `p95`, `max`, the declared ceiling from the resolved provider profile, `censored` (true when
`max >= 0.95 * ceiling` or any call of that pair ended `error_max_budget_usd`), and the three candidate
bounds: `strict = p50`, `moderate = p95`, `liberal = 3 * p95`. A pair with `n < 20` prints `insufficient`
and no candidates. `--write PATH` writes the table as JSON `{generated, db, rows: [...]}`; nothing else in
the tool edits a profile — bounds reach the profile through a PR, on purpose.

## 3. Levels say what a stop means  (cartridges, graphs)

- `cartridges/base/cartridge.yaml` gains `policy.cost: {level: moderate, bounds: null}` with the meaning
  table beside it: `strict` — a stop means mis-shaped, split it; `moderate` — unusual, look at it;
  `liberal` — something is wrong. `bounds` names a JSON file written by §2 (path relative to the cartridge
  dir), or null.
- The harness resolves a node's shape ceiling as: the bounds file's `<level>` figure for `(role, model)`
  when the file exists and that pair has `n >= 20`; otherwise the provider profile's `role_budget_usd` /
  `budget_usd` (the unmeasured default, which is deliberately generous). The resolved ceiling and its
  source (`bounds:<level>` or `profile`) are recorded on the call record, so a stop can be read back.
- `core/cartridge.py` validates `policy.cost.level` against the three names. The project layer may set
  `policy.cost.level` under the existing tighten-only rule (strict < moderate < liberal).

## 4. Where the money goes  (tools)

`cox stats spend-mix [--json]`: per model, the token counts and the cost share by class
(`output`, `cache_creation`, `cache_read`, `input`) at the profile's list rates, plus the same split for
`build` alone. Measurement only — it exists so P4 tunes the right half of the bill. Rates come from one
table in `agent_tools/stats_derive.py`, named per model id, with a test pinning the 2026-09 list prices.

## 5. Out of bounds

Changing any ceiling number in the provider profile (numbers move by PR with a `cox stats bounds` table
pasted in); per-ticket `budget_usd` (already exists, capped by `build_budget_usd_max`); the router (P4).

## 6. Rules, one literal test each

1. `gather` receives `window_ceiling_usd` from the profile at both call sites; absent block -> `None`.
2. `min(shape, node_cap)` is the effective limit; a stop at the cap is `error_spend_cap`, at the shape
   ceiling `error_max_budget_usd`; the message carries both numbers.
3. `bounds` marks `censored` when max is within 5% of the ceiling, and when any call budget-stopped.
4. `bounds` prints `insufficient` and no candidates for `n < 20`.
5. `strict/moderate/liberal` equal `p50 / p95 / 3*p95` on a literal distribution.
6. The harness takes the bounds figure at the configured level when `n >= 20`, else the profile's, and
   records the source.
7. `policy.cost.level` rejects an unknown name; the project layer may tighten, never loosen.
8. `spend-mix` cost shares sum to 1.0 per model and use the pinned rates.
