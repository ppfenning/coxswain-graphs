# The observed record

Status: approved by the chair 2026-09-08 under Pat's standing principle (2026-09-07): "the goal is to
have nothing in the system become invisible." Groups intake items G1 from
`workspace/plans/agent-platform/2026-09-08-intake-grouping.md`. This file is committed identically to
`coxswain-graphs/docs/design/` and `coxswain-tools/docs/design/` so a build seat in either repo can read it.

## 0. The defect, once

The platform NARRATES what it already OBSERVES. It asks a model to report the files it touched while
holding the patch; asks it to report the commands it ran while the trace recorded every one; summarises
call costs in memory at an end that a dying run never reaches; infers a "budget stop" by substring-matching
rendered prose; and files four different outcomes under one word, `quarantined`. Five sites, one rule:

> Write a fact where it happens, in the shape it has there. Derive, never ask. Where nothing was observed,
> record `unknown` with its source, never a guess and never nothing.

Two mechanical corollaries. (a) **No exit path leaves no record**: bookkeeping runs on failure paths.
(b) **Every derived value names its source**; every refusal names a defect.

## 1. Per-call ledger, written as calls return  (graphs)

Today `runner/claude_code_runner.py:590` appends a call dict to `self.calls` in memory, and
`harness/usage.py:45 record_usage` writes `<runs_dir>/<run_id>.usage.json` from that list — called once
at `harness/cli.py:390` (epic) and `:656` (single graph), only on the success path. A run that dies on a
budget stop or a `RunnerError` writes nothing; 101 calls worth $29.20 were recoverable only from traces.

Change:
- The runner appends the same dict, plus `"ts"` and `"ok": bool` (and `"error"` on failure), as one line to
  `<runs_dir>/<run_id>.calls.jsonl` at the moment the call returns — in the success branch at `:590` AND in
  the `RunnerError` path (`:571`-`:577`, where the failed trace is renamed `.error.jsonl`; the payload's
  `total_cost_usd` is known there). The runner learns `runs_dir`/`run_id` the way it learns `trace_dir`.
- `record_usage` reads that file when it exists, unions it with `runner.calls` by `(role, trace)`, and writes
  `usage.json` as today; the file, not memory, is the source. It runs in a `finally` on both cli paths so a
  run that raises still leaves `usage.json`.
- `summarize` unchanged; `usage.json` shape unchanged except each call may carry `ok`, `error`, `ts`.

## 2. Evidence derived, not self-reported  (graphs)

`BUILD_SCHEMA` (`graphs/delivery/lifecycle_propose.py:103`-`:120`) REQUIRES `files_touched` and
`commands_run`. Four runs died with `error_max_structured_output_retries` naming exactly those two fields
after producing a patch; six refusals cited missing `commands_run` when the trace showed the check was run.

Change:
- Both fields become OPTIONAL in `BUILD_SCHEMA`; the harness derives them and the derived value wins.
- `files_touched` := the `+++ b/<path>` (and `--- a/<path>` for deletions) paths of the patch. Put it next to
  the line counts in `change_facts` (`:362`-`:380`), which already counts from the patch "rather than asked".
- `commands_run` := every `Bash` tool call in the node's trace (`runs/<run>-trace/<role>-<n>.jsonl`) paired
  with its tool result: `[{"command", "output", "source": "trace"}]`. A pure function
  `trace_commands(lines: Sequence[Mapping]) -> list[dict]` over the parsed trace lines lives in the runner
  module; the harness calls it with the trace path the call record already carries. Model-supplied entries
  are kept only when no trace exists, tagged `"source": "self_report"`.
- `measured_facts` (`:385`-`:405`) and the evidence normalisation (`:1637`-`:1642`) read the derived lists.
  The `handoff` and `validate_chunk` nodes therefore see observed commands; a genuinely un-run check is now
  provable at build return, not after a review round.

## 3. One word, four outcomes  (graphs, then tools)

Measured over every run: fix-loop refusals 131, budget/plan deaths 64, `validate_chunk` unsatisfied 28
(26 with a patch and an approving review+arbitration chain), attempt cap 23, handoff incomplete 18,
phase-infra failures 14. All reach the ledger and the stats store as `quarantined`.

Change: `_quarantine_task` (`harness/epic.py:608`) takes `kind: str` and writes it into the phase record entry,
the printed line (`quarantined task [<kind>]: <id> — <reason>`), and `record_attempt` onto the work item
(`attempts[].kind`). Kinds, closed set:

| kind         | meaning                                              | sites today                          |
|--------------|------------------------------------------------------|--------------------------------------|
| `refused`    | reviewers/arbiter rejected the patch                 | `epic.py:890`                        |
| `no_work`    | nothing produced: budget death, attempt cap, handoff incomplete | `:763`, `:856`, plan/budget paths |
| `unverified` | approved chain, validator unsatisfied — PATCH KEPT    | `:1011`                              |
| `infra`      | nothing ran: worktree/branch failure                  | `_open_phase_worktree` `:276` callers |

`unverified` never discards: the task record keeps `build.patch` (it already does) and the phase record entry
carries `"patch_kept": true` so `cox runs land` can offer it. `infra` is NOT a task outcome: it is recorded on
the phase as `phase_failed_to_start` with the reason, and the tasks stay `ready` untouched. The two
`quarantined.append` sites that bypass `_quarantine_task` (`:754`, `:936`) go through it.

## 4. Consumers read the record, not the prose  (tools)

- `agent_tools/events.py:51` substring-matches `error_max_budget_usd` anywhere in a line; all nine bare
  `budget_stop` events in the corpus are false positives (quoted code, reviewer prose). Delete the branch, or
  anchor it to the literal emitter line `^\s*node '.+' failed in .+: error_max_budget_usd`. Never add ticket
  capture to it: real budget stops already arrive as `quarantined task` lines with a ticket.
- `stats_ingest.load_run` reads `<run>.calls.jsonl` as the second source after `usage.json` and before trace
  recovery; a call recovered from it carries `source: calls_jsonl`.
- `tasks` gains `outcome_kind TEXT` (the §3 kind, from `attempts[].kind` or the phase record; `NULL` for
  records written before this lands — never inferred from the reason text). `roles_report` shows
  `unverified` and `infra` separately from `refused` so a landed_rate is never depressed by infrastructure.
- `cox stats coverage [--json]`: one row per provenance question, `known / total` and the fraction —
  runs with provider_profile, runs with host, runs with cartridge_sha, calls with a task, calls with a
  failure_class, tasks with a known outcome, tasks with an outcome_kind. Expected to trend toward 1.0; the
  report is the number the principle is judged by.

## 5. Out of bounds

No change to review or validation judgement; no new nodes; no relaxing of the evidence requirement (only
of who supplies it); no schema migration of existing rows beyond adding nullable columns; no writes to
`ledger.jsonl`. `failure_class` role-scoping (G1 item 4) already landed in coxswain-tools and is retired.

## 6. Tests each phase must carry

§1 a `RunnerError` still appends a line and `record_usage` still writes `usage.json`. §2 a trace with two
Bash calls yields two `commands_run` entries with `source: trace`; a patch touching `a.py` and deleting
`b.py` yields both in `files_touched`; a build payload lacking both fields validates. §3 each kind is
emitted from its site; an `unverified` entry carries `patch_kept`. §4 a log line quoting the budget string
in prose yields no event; a `calls.jsonl`-only run ingests with its cost; coverage over a three-run literal
corpus prints seven rows.
