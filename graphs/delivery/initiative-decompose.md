# initiative-decompose — specification

The front of the pipeline. An idea arrives as prose; phases, tasks, and the
dependency edges between them come out. Everything downstream — what can run in
parallel, what has to wait — is decided here.

| Node | Role | Tier | Notes |
|---|---|---|---|
| `decompose` | `decompose` | standard | idea → phases → tasks, with edges and surfaces |
| `adversary` | `review_adversary` | deep | **attacks the dependency edges** |
| `emit` | — | — | one `item_create` proposal per task |

**Args:** `run_id`, `date`, `cartridge` (resolved, required, no fallback), `idea`.

**Returns:** `{run_id, date, idea, shape, phases, tasks, challenge, proposals[], totals}`

## The adversary is pointed at the edges, deliberately

This is the highest-leverage adversarial pass in the system, and it has one job:
find dependency edges that are not real.

Every spurious edge silently serialises work that could have run at the same
time, and nothing downstream will ever question it — the phase runner simply
sees a blocked task and waits. The person who just drew the graph is also the
last person likely to notice they drew too many edges, which is exactly the
situation a second, hostile reader is for.

It works in both directions. Dropping a false edge buys parallelism; adding a
real one that was missed prevents a task starting on ground that is not there
yet, which is the failure that parallelism would otherwise cause. Two things it
is not allowed to do: invent a task that does not exist, and make a task depend
on itself.

## Refusing to emit nonsense

A cycle means nothing in it can ever become ready — the phase would stall with
no explanation — so a decomposition containing one is **refused before anything
is proposed**, and refused again by the work store on read. Both checks exist
because the failure is silent and the cost of finding it late is a stalled phase
nobody can diagnose.

`totals` reports `edges_dropped` and `immediately_startable`, which is the
number worth watching: it is how much of the phase can begin at once.

**Status:** implemented in [`initiative_decompose.py`](initiative_decompose.py).
