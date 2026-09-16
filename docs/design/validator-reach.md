# Validator reach

Status: approved by the chair 2026-09-15 under Pat's standing order (2026-09-08: "work on Gs then Ps,
keep moving until complete") and his direction that "removing human gates in places like that is the
exact goal". Groups intake G4 from `workspace/plans/agent-platform/2026-09-08-intake-grouping.md`:
`validate-chunk-reads-an-evidence-array-no-build-can-write-to`,
`validate-chunk-refused-with-reasoning-that-names-no-defect`,
`no-seat-can-analyse-the-workspace-corpus`. Committed to `coxswain-graphs` under `docs/design/`; the
cartridges half (the `validate-chunk` skill text) cites this file.

## 0. The defect, once

A seat is asked for a verdict on evidence outside its reach, and the verdict is treated as if it had been
inside it. Measured 2026-09-08: 31 of 176 quarantines discarded a patch every reviewer had approved.
Three refusals of one ticket ($12) came from `validate_chunk` reading a harness-built `evidence` array
that no build can write to, while the commands it demanded sat in `build.commands_run`. One refusal
(`coxswain-releasability`) described a correct diff in every sentence and returned `unsatisfied` with no
defect named. And any ticket whose answer lives in `workspace/runs` or the ledger is unroutable, because
the only seat that runs commands sees one repository worktree.

> The validator judges what the harness observed, a refusal that names no defect is not a refusal, and a
> question the build seat cannot reach is refused before dispatch, not after a run.

## 1. The evidence array carries what the trace saw  (graphs)

`docs/design/observed-record.md` §2 already derives `files_touched` and `commands_run` from the runner's
own call ledger (landed: `runner/claude_code_runner.py`, `trace_commands`). This section puts them where
the validator reads.

- In `harness/epic.py` where the task record's `evidence` list is assembled (the `patch_apply` and
  `checks:*` entries, `_record_task`/`checks_evidence` neighbourhood), append one entry per trace-observed
  command: `{"check": "command", "source": "trace", "output": "<command>\n<output tail, 20 lines>"}`, and
  one `{"check": "files_touched", "source": "trace", "output": "<path per line>"}`. Entries come from the
  build call's `commands_run` where `source == "trace"`; a self-reported command (`source: self_report`)
  is NOT folded in — the validator must never be shown a claim as an observation.
- `graphs/delivery/phase_validate.py` (`chunk_prompt`, the brief `validate_chunk` reads): the validation brief states, in one sentence, that every
  `command` entry was observed by the harness, that there is no other place a build's commands can appear,
  and that a ticket requirement for a command is met by a matching `command` entry.
- Order is preserved (the trace's order), so "ran the tests after the edit" is checkable.

## 2. A refusal names a defect or it is not a refusal  (graphs, cartridges)

- `validate_chunk`'s output schema gains `defects: [{claim, where: {file, line?}, evidence_ref?}]`.
  `unsatisfied` with an empty `defects` list is **malformed**. `where.file` names a path in the diff or an
  `evidence` entry's `check`; a claim that names neither is also malformed.
- The harness (`graphs/delivery/phase_validate.py`, at the `validate_chunk` site — its same-role retry is the pattern) handles malformed like a placeholder review:
  one retry of the same node with the malformation quoted back ("your refusal names no defect"); a second
  malformed answer is recorded on the task record as `validation: {verdict: abstained, reason:
  "malformed refusal x2"}` and the task proceeds on the reviewers' verdicts. An abstention never discards
  an approved patch; it is visible in the record and counted by `cox stats explain`.
- A well-formed `unsatisfied` behaves exactly as today.
- The `validate-chunk` skill (cartridges) says so in its Discipline: "name the defect, with a file, or do
  not refuse".

## 3. The corpus is out of the build seat's reach, and lint says so  (graphs)

Decision: (b) from the intake — analysis of the platform's own record is NOT build work. The deterministic
surface for those questions is `cox stats` (roles, explain, coverage, series); what it cannot answer the
chair answers by hand and files as a stats gap. A read-only analyst seat is deferred to P4 (filed).

- The `reach` rule of `lint_tickets` (`docs/design/work-shape.md` §3, `graphs/delivery/ticket_lint.py`)
  gains the corpus paths: a ticket whose body or surfaces name `workspace/`, `runs/`, `*.usage.json`,
  `ledger.jsonl` or `~/.local/state` is refused with the correction "route this to `cox stats` or the
  chair; the build seat sees one repository worktree".
- `docs/RUNBOOK-TRUST.md` gains a short "What a seat can see" paragraph stating the same.

## 4. Out of bounds

Changing what the reviewers see (§1 is the validator's array only); any new seat or role; changing the
placeholder-review handling (§2 reuses it); the `commands_run` derivation itself (G1 owns it).

## 5. Rules, one literal test each

1. A build call with two trace commands yields two `command` evidence entries in trace order, each
   `source: trace`, and zero for a self-reported one.
2. A `files_touched` entry lists exactly the paths the derived record carries.
3. `unsatisfied` with `defects: []` is malformed; the node is retried once with the malformation quoted.
4. Two malformed refusals record `validation.verdict == "abstained"` and the task is not quarantined.
5. `unsatisfied` with one defect naming a diff file quarantines as before.
6. A defect whose `where.file` names neither a diff path nor an evidence check is malformed.
7. `lint_tickets` refuses a ticket naming `workspace/runs` under `reach` with the stated correction, and
   passes the same ticket with the path removed.
