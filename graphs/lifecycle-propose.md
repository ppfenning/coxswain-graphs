# lifecycle-propose — specification

The development loop. Takes one ticket, produces reviewed work and proposals.
Nothing is pushed, opened, or merged.

**Nodes** (v0 cut — keep every node that reads the cartridge, drop every node
that only reads other nodes):

| Node | Role | Tier | Writes |
|---|---|---|---|
| `scope` | `scope_epic` | standard | none (read-only, **optional role**) |
| `plan` | `plan` | standard | none (read-only) |
| `build` | `build` | standard | its own worktree only |
| `review` | `review_charter` | deep | none |
| `emit` | — | — | proposals as data |

`scope` runs first and only if the team bound `scope_epic` — unbound means absent,
which is what an optional role means. It applies the cartridge's `epic_threshold`
to decide epic / parent-with-subtasks / single ticket, routes the result through
`ticket_routing`, and emits a `ticket_create` proposal carrying both decisions as
evidence. Scoping is a separate act from filing, and it runs before planning
because it decides whether this is even one ticket.

The table says which node writes what. What it cannot show is *where the write
actually happens* — the build node produces a patch and applies nothing, and the
shell applies it, on the far side of the gate:

```mermaid
flowchart TB
    TICKET["ticket (an arg)"] --> PLAN

    subgraph GRAPH["the graph — pure, no disk, no clock"]
        SCOPE["scope<br/><i>role: scope_epic · standard</i><br/>epic_threshold + ticket_routing"]
        PLAN["plan<br/><i>role: plan · standard</i>"]
        BUILD["build<br/><i>role: build · standard</i>"]
        REVIEW["review<br/><i>role: review_charter · deep</i>"]
        FACTS["change_facts<br/><i>counted from the patch,<br/>not asked of the model</i>"]
        EMIT["emit"]

        SCOPE --> PLAN
        PLAN --> BUILD
        BUILD -- "unified diff<br/>(returned, not applied)" --> FACTS
        FACTS --> REVIEW
        REVIEW --> EMIT
        SCOPE -. "ticket_create proposal" .-> EMIT
    end

    EMIT -- "proposals" --> POLICY{"autonomy_policy<br/><i>has this kind graduated?</i>"}
    POLICY -- "propose" --> GATE{{"human gate"}}
    POLICY -- "auto" --> ARM["apply arm (a role)"]

    subgraph SHELL["shell.py — the only side effects"]
        APPLY["git apply, in a worktree<br/>the shell created"]
        RECORD["build_manifest → record_run"]
    end

    GATE -- "approved" --> APPLY
    GATE -- "every decision" --> RECORD

    APPLY -.->|"never"| PUSH["push · open PR · merge"]

    style PUSH stroke-dasharray: 5 5
```

The dashed edge is the point of the graph: a draft PR is *emitted as a proposal*
and never executed, and nothing is pushed or merged by any path through this
diagram.

**Args:** `run_id`, `date`, `cartridge` (resolved, required, no fallback),
`ticket`, optional `worktree_root` (else `cartridge.landing_areas.worktree_root`).

**Returns:** `{run_id, date, ticket, plan, build, review, change_facts, proposals[]}`

**Deferred from v0:** intake queue (the ticket arrives as an arg), the
adversarial reviewer pair, arbitration, the bounded fix loop, verification,
retro. Staging a draft PR is *emitted* as a `draft_pr_create` proposal and never
executed.

*Epic-threshold scoping was on that list and is now implemented* — `epic_threshold`
and `ticket_routing` were declared in the base cartridge and read by no code,
which is exactly the drift the cartridge seam exists to prevent.

**Status:** implemented in [`lifecycle_propose.py`](lifecycle_propose.py). The
build node returns a unified diff and applies nothing; `shell.py` applies it in
a worktree it owns, and only after the gate approved the work.
