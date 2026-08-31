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

The sequence is the least interesting thing here. What matters is the two caps
and the fact that neither one drops anything on the floor:

```mermaid
flowchart TB
    QUEUE["alerts (an arg — the graph<br/>does not read the queue)"]

    QUEUE --> FETCH

    FETCH{"fetch<br/>cap: max_alerts (15)"}
    FETCH -- "over the cap" --> OVER["deferred_overflow<br/><i>counted, returns next run</i>"]
    FETCH -- "within the cap" --> CLASSIFY

    CLASSIFY["classify<br/><i>role: triage_classify · cheap</i><br/>against the runbook index"]

    CLASSIFY --> VCAP{"cap: verify_cap (5)"}
    VCAP -- "over the cap" --> CAPD["deferred_for_capacity<br/><i>classified, not verified</i>"]
    VCAP -- "within the cap" --> VERIFY

    VERIFY["verify<br/><i>role: evidence_verify · deep</i><br/>runbook checks, run verbatim<br/>+ the trap for this symptom"]

    VERIFY -- "actionable, with checks" --> EMIT["emit<br/>proposals + evidence"]
    VERIFY -- "not actionable" --> QUIET["no proposal"]

    OVER --> TOTALS
    CAPD --> TOTALS
    EMIT --> TOTALS
    QUIET --> TOTALS

    TOTALS[["totals: received, fetched, verified,<br/>deferred_overflow, deferred_for_capacity"]]
```

Every path ends at `totals`. That is deliberate: a graph that drops nine of ten
alerts and reports success on the tenth is worse than one that fails, so both
caps report what they set aside rather than truncating silently. Note also that
only a *verified* alert can produce a proposal — an unverified one has no
evidence, and a claim without evidence is not a proposal.

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
