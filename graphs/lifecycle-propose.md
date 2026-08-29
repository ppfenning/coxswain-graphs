# lifecycle-propose — specification

The development loop. Takes one ticket, produces reviewed work and proposals.
Nothing is pushed, opened, or merged.

**Nodes** (v0 cut — keep every node that reads the cartridge, drop every node
that only reads other nodes):

| Node | Role | Tier | Writes |
|---|---|---|---|
| `plan` | `plan` | standard | none (read-only) |
| `build` | `build` | standard | its own worktree only |
| `review` | `review_charter` | deep | none |
| `emit` | — | — | proposals as data |

**Args:** `run_id`, `date`, `cartridge` (resolved, required, no fallback),
`ticket`, optional `worktree_root` (else `cartridge.landing_areas.worktree_root`).

**Returns:** `{run_id, date, ticket, plan, build, review, change_facts, proposals[]}`

**Deferred from v0:** intake queue (the ticket arrives as an arg), epic-threshold
scoping, the adversarial reviewer pair, arbitration, the bounded fix loop,
verification, retro. Staging a draft PR is *emitted* as a `draft_pr_create`
proposal and never executed.

**Status:** unimplemented. See the repo README.
