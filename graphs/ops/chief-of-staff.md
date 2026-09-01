# chief-of-staff — specification

Deferred from v0 as "a graph whose nodes dispatch other graphs" (see
README.md). It turns out to be one node, because the hard part was never the
model call — it was drawing the line between deciding what to run and
actually running it, cleanly enough that the decision stays a graph.

| Node | Role | Tier | Notes |
|---|---|---|---|
| `dispatch` | `dispatch` | standard | selects from the docket's registry, or declares idle |

**Args:** `run_id`, `date`, `cartridge` (resolved, required, no fallback),
`docket` (required — see shape below; the `harness/cos.py` driver assembles
it, so the graph's own `Need` is not required at the CLI).

**Docket shape:**

```
{
  registry: [{name, summary, runnable: bool, reason: str}],
  intake:   [{id, kind, title}],
  ready_tasks: [str],
  ledger:   {rows: int, agreement: float | null},
}
```

**Returns:** `{run_id, date, selections[], idle: bool, reasoning, proposals: []}`

`proposals` is always `[]`. This graph does not propose anything of its own —
see "Invocation is the harness's" below.

## Dispatch is judgment; invocation is consequences

Two different kinds of thing were fused in the phrase "chief-of-staff
dispatch," and separating them is the whole design:

- **Deciding what to run next is judgment.** It weighs a registry, a queue, a
  ledger's shape, and produces a reasoned answer — exactly the kind of thing
  every other node in this system is asked to do. So it lives in a graph: one
  node, the `dispatch` role, pure, replayable, no disk, no clock, like
  everything else under `docs/GRAPH-CONTRACT.md`.

- **Actually running what got selected is not judgment, it is I/O.** It
  blocks on futures, waits on results, and touches whatever the selected
  graphs touch. `harness/cos.py` — not a graph, and it carries no `SPEC` —
  does this half: it builds the docket this graph reads (`assemble_docket`),
  and after the graph decides, it turns each selection into that graph's
  constructed args and invokes them via `harness.invoke.invoke_graphs`
  (`run_cos`), under **one run id** derived from the parent. That is the same
  nested-invocation primitive `harness/phase.py` already runs a whole phase
  through — `phase`, chief-of-staff, and the bounded fix loop are its three
  named consumers, and this is the second one to arrive.

Because the dispatched graphs run under the parent run id, their proposals
flow into the **same** policy/gate/ledger path as anything else. A
chief-of-staff run's proposals are its children's proposals, assembled by the
driver in invocation-id order — never wall-clock order, for the reason
`harness/invoke.py` already gives: two runs over the same selections must
produce the same record no matter who finished first.

## The graph enforces "never dispatch past absent inputs"

A skill can *tell* the model not to select an unrunnable graph. Only the graph
can make that true regardless of what the model does. Every selection is
checked against the docket's own registry:

- naming an entry the registry does not mention at all — refused
- naming an entry marked `runnable: false` — refused

Both raise `ContractViolation`, before anything downstream sees the
selections. The driver that eventually invokes them never has to trust a
model's belief about what exists or what is ready — it can trust that a
selection that reached it already survived this check.

`idle: true` together with a non-empty `selections` is refused as incoherent:
either nothing needs running, or something does.

## An empty docket is a legitimate answer

Nothing here treats "there is nothing to dispatch" as a failure mode or a
special case to route around. `idle: true`, no selections, is exactly as valid
a return as any list of them — a day with nothing queued, no ledger rows yet,
and no alerts in hand is a day where the honest answer is "there is nothing
that needs doing right now," and the docket, the schema, and the driver all
say so plainly rather than manufacturing something to run.

**Requires** the optional `dispatch` role. A team that has not bound it is
told so, rather than silently getting a chief of staff that does nothing.

**Status:** implemented in [`chief_of_staff.py`](chief_of_staff.py) (the
graph) and `harness/cos.py` (the driver).
