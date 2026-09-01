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
`work_routing`, and emits a `item_create` proposal carrying both decisions as
evidence. Scoping is a separate act from filing, and it runs before planning
because it decides whether this is even one ticket.

The table says which node writes what. What it cannot show is *where the write
actually happens* — the build node produces a patch and applies nothing, and the
shell applies it, on the far side of the gate:

```mermaid
flowchart TB
    TICKET["ticket, an argument"] --> SCOPE

    subgraph GRAPH["the graph: pure, no disk, no clock"]
        SCOPE["scope<br/>role: scope_epic, standard<br/>epic_threshold and work_routing"]
        PLAN["plan<br/>role: plan, standard"]
        BUILD["build<br/>role: build, standard"]
        FACTS["change_facts<br/>counted from the patch,<br/>never asked of the model"]
        HANDOFF{"handoff<br/>does build's output contain<br/>what review needs?"}
        REVIEW["review<br/>role: review_charter, deep"]
        ADV["adversary<br/>role: review_adversary, deep"]
        ARB["arbitrate<br/>role: arbitrate, deep"]
        EMIT["emit"]
        STOP(["graph stops"])

        SCOPE --> PLAN
        PLAN --> BUILD
        BUILD -- "unified diff, returned not applied" --> FACTS
        FACTS --> HANDOFF
        HANDOFF -- "incomplete" --> STOP
        HANDOFF -- "complete, with a small brief" --> REVIEW
        REVIEW -- "review_tier 0" --> EMIT
        REVIEW -- "review_tier 1 or more" --> ADV
        ADV -- "agreed, tier 1" --> EMIT
        ADV -- "disagreed, or tier 2" --> ARB
        ARB --> EMIT
        SCOPE -. "item_create proposal" .-> EMIT
    end

    EMIT -- "proposals" --> POLICY{"autonomy_policy<br/>has this kind graduated?"}
    POLICY -- "propose" --> GATE{{"human gate"}}
    POLICY -- "auto" --> ARM["apply arm, a role"]

    subgraph HARNESS["harness: the only side effects"]
        APPLY["git apply, in a worktree<br/>the harness created"]
        RECORD["build_manifest, then record_run"]
    end

    GATE -- "approved" --> APPLY
    ARM --> APPLY
    GATE -- "every decision" --> RECORD

    APPLY -. "never" .-> PUSH["push, open a PR, merge"]

    style PUSH stroke-dasharray: 5 5
```

Two things the node table cannot show. The dashed edge is the point of the
graph: a draft PR is *emitted as a proposal* and never executed, and nothing is
pushed or merged by any path. And `handoff` has an edge that leaves the graph
entirely — an incomplete handoff stops the run rather than letting review form a
confident opinion about a half-finished change.

How many reviewers a change gets is `review_tier`'s decision, not the author's:
tier 0 is reviewed once, tier 1 gets an adversary, and tier 2 arbitrates whether
or not the two agreed. No path skips review.

## The fix loop is bounded, and it counts

A change the reviewers sent back goes back to the builder with the critique
attached — the charter findings, the adversary's objections, the arbitrator's
reasoning — and the instruction that every standing objection must actually
fall. The retry is then reviewed under *exactly* the same rules as the first
try: facts recounted from the new patch, handoff re-checked if it is bound, tier
recomputed, the same reviewers at the same tier. A cheaper second pass would be
a way of grinding a change past its reviewers, which is the thing this loop must
not become.

It is bounded three separate ways, and each stop is recorded by name:

| Stop | When | Why it is a stop |
|---|---|---|
| `no_progress` | successive patches are ≥ 0.98 similar (`difflib.SequenceMatcher`) | Re-submitting the same diff is not a fix; it is shopping for a verdict, and eventually one reviewer says yes. The near-identical patch is never reviewed. |
| `objection_standing` | a retry's adversary raises a claim already standing (matched case-insensitively, stripped) | Re-litigating an objection is not progress. A retry that instead ends in `approve` means the reviewers, shown the standing objections, judged them fallen. |
| `attempts_exhausted` | `fix_attempts` additional attempts (default 2) produced no approval | A cap that can be argued with is not a cap. |

The similarity check is `difflib` — pure, no disk, no clock — so it stays inside
the graph rather than becoming another thing the shell has to do.

**And the loop refuses to hide the count.** `fix_loop.attempts` is on every run;
a proposal that took more than one attempt carries `attempts` and an evidence
row saying which attempt approved it. A task that passed on the third try is not
the same evidence as one that passed clean, and the difference has to survive
the trip downstream: the policy in the substrate is what refuses to let a
repeated-attempt pass extend a streak, and it can only refuse what it can see.
This graph's job is not to enforce that rule — it is to never quietly make the
record look better than the run was.

**Args:** `run_id`, `date`, `cartridge` (resolved, required, no fallback),
`ticket`, optional `fix_attempts` (default 2; `0` disables retries), optional
`worktree_root` (else `cartridge.landing_areas.worktree_root`).

**Returns:** `{run_id, date, ticket, plan, build, review, change_facts, fix_loop,
proposals[]}` — `plan`/`build`/`review` hold the final round's values.

**Deferred from v0:** intake queue (the ticket arrives as an arg), the
adversarial reviewer pair, arbitration, verification, retro. Staging a draft PR
is *emitted* as a `draft_pr_create` proposal and never executed.

*Epic-threshold scoping was on that list and is now implemented* — `epic_threshold`
and `work_routing` were declared in the base cartridge and read by no code,
which is exactly the drift the cartridge seam exists to prevent.

**Status:** implemented in [`lifecycle_propose.py`](lifecycle_propose.py). The
build node returns a unified diff and applies nothing; `shell.py` applies it in
a worktree it owns, and only after the gate approved the work.
