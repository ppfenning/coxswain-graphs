# triage-propose — specification

Morning triage of an alert queue. Strictly read-only everywhere.

| Node | Role | Tier | Notes |
|---|---|---|---|
| `fetch` | — | standard | open alerts from the queue; capped, overflow counted not dropped |
| `classify` | `triage_classify` | cheap | symptom key per alert, against a runbook index |
| `verify` | `evidence_verify` | deep | runbook-guided; deterministic checks run verbatim |
| `emit` | — | — | proposals with evidence attached |

**Args:** `run_id`, `date`, `cartridge` (required, no fallback), optional
`max_alerts` (default 15).

**Runbook index** is a cartridge-provided path, not a skill-layout assumption.
Each entry carries `match`, the `kinds` it may emit, a `risk`, and — most
usefully — the `trap`: the known wrong belief for that symptom. A runbook that
only states the right answer lets the next person re-derive the wrong one.

**Fetch cap** must exceed the verify cap comfortably; a busy queue otherwise
blows the structured-output limit and the run dies mid-flight. Overflow defers
to the next run and is reported in the totals.

**Status:** implemented in [`triage_propose.py`](triage_propose.py). Alerts
arrive as an argument — the graph does not read the queue itself, because a node
that fetches cannot be replayed. Both caps report their overflow in `totals`.
