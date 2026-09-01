# retro-propose — specification

The ledger reads back. A retro that reasoned from memory would be exactly the
self-report the ledger exists to disbelieve, so this graph proposes nothing
it cannot point at a row for.

| Node | Role | Tier | Notes |
|---|---|---|---|
| `stats` | — | — | deterministic bucket arithmetic over the ledger's rows |
| `retro` | `retro` | deep | reasons only about buckets already established as fact |
| `emit` | — | — | `doc_update` proposals, cited and substantiated |

**Args:** `run_id`, `date`, `cartridge` (resolved, required, no fallback),
`ledger_rows` (the parsed ledger, required — may be an empty list), optional
`max_proposals` (default 5).

**Returns:** `{run_id, date, observations[], proposals[], totals}`

Ledger rows arrive as an argument, the same as every other graph's inputs. The
harness reads the file the `ledger_rows` need names and hands the graph parsed
rows; the graph never reads its own trust record — a proposal about the
ledger produced by code that also reads the ledger live is not an independent
check.

## Why the stats are not asked of the model

`stats` runs before the model sees anything, per `(kind, subject-or-None)`
bucket: outcome counts, the current consecutive-clean streak, the reversal
count, and how many cleans took more than one build attempt. The streak
follows the exact rule `core.policy` uses to decide graduation — a clean that
took several attempts is transparent, proving the loop converged rather than
that the kind is trustworthy first-try — so retro's idea of "the current
streak" never disagrees with the policy that actually spends it.

The `retro` node then reasons only about numbers already established as fact.
Its answer is graded on the way out: every observation and every proposal
carries `cites`, naming the bucket key(s) (`"<kind>|<subject-or-->"`) that
support it. A cite naming a bucket the stats do not contain is refused as a
**fabricated citation**. A proposal with **zero cites** is refused outright —
retro's whole discipline is that claims cite rows, and a claim that cites
nothing is a guess with formatting. The graph then builds each proposal's
`evidence` **from the stats for the cited bucket(s)** — the model cites, the
graph substantiates.

Zero ledger rows returns `{observations: [], proposals: [], totals: {rows: 0, ...}}`
with **no node call**. A retro over nothing has nothing to learn.

**Requires** the optional `retro` role. A team that has not bound it is told
so, rather than silently getting a graph that runs and finds nothing.

**Caps:** at most `max_proposals` proposals emit; the rest are counted in
`totals.deferred_overflow`, never dropped. Fabricated-citation and zero-cite
refusals happen before the cap is applied — a proposal has to be honest before
it gets to compete for a slot.

## Scope limit, stated honestly

`docs/GRAPH-CONTRACT.md`'s proposal shape requires a `risk`, read off the
cartridge's write-kind taxonomy. In the base taxonomy
(`cartridges/base/cartridge.yaml`), `charter_proposal` and `skill_proposal`
declare **no** `risk` — they are `{ramp: never, apply_arm: pr}` and nothing
else — and `graphs._contract.proposal` refuses to build a proposal for any
kind without one. That is precisely the finding retro would sometimes want to
make: "the charter got this wrong," or "a skill needs rewriting because of
what the last forty runs show."

Rather than inventing a risk at the node — the exact move the contract exists
to forbid — this graph emits `doc_update` proposals only in v1, and reports
any charter- or skill-level finding as an `observations` entry instead: cited
the same way a proposal is, but data, not a proposal, and not routed through
the gate. This is **emission blocked on the taxonomy carrying a risk for
`charter_proposal`/`skill_proposal`**, not a judgment that those findings
don't matter. The day the taxonomy grows a risk for them, `observations`
graduates the same finding shape into real proposals with no change to how
the node reasons — only to what `emit` is allowed to do with the answer.

**Status:** implemented in [`retro_propose.py`](retro_propose.py).
