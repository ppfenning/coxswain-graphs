# epic-reconcile — specification

The feedback half of epics. `lifecycle-propose` scopes work *into* an epic;
this checks whether the epic still describes reality afterwards. Strictly
propose-only.

Epics drift in ordinary ways: a ticket gets closed on the board and not in the
epic, a phase finishes out of order, work gets added that nobody attached. An
epic model that is only ever written and never checked degrades into a diagram
of what someone once intended.

| Node | Role | Tier | Notes |
|---|---|---|---|
| `compare` | — | — | deterministic set arithmetic over declared vs observed |
| `reconcile` | `reconcile` | standard | which differences are drift, and what corrects each |
| `emit` | — | — | `ticket_update` / `board_move` proposals |

**Args:** `run_id`, `date`, `cartridge` (resolved, required, no fallback),
`epic` (the declared state), `observed` (the board's actual state).

**Returns:** `{run_id, date, epic, divergences, reconcile, proposals[], totals}`

Both sides arrive as arguments. A reconcile that fetches its own "actual"
cannot be replayed, and a drift report you cannot replay is one you cannot
argue with.

## Why the comparison is not a model call

`compare` is set arithmetic, and it runs before the model sees anything. A node
asked "what drifted?" will produce a plausible answer whether or not anything
did; a set difference will not. The `reconcile` node then reasons only about
differences that are already established fact, and a correction naming a ticket
the comparison never flagged is **refused** rather than proposed — the node does
not get to invent drift.

When nothing has drifted, no model call happens at all. Asking a model to
confirm that nothing happened is a good way to be told that something did.

`correction: none` is a first-class answer. Not every difference is drift; some
are legitimate, and a reconcile pass that cannot say so trains people to ignore
it.

**Requires** the optional `reconcile` role. A team that has not bound it is told
so, rather than silently getting a graph that does nothing.

**Status:** implemented in [`epic_reconcile.py`](epic_reconcile.py).
