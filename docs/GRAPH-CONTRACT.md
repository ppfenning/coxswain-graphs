# What a graph must satisfy

A graph is an ordered set of agent nodes that reads a cartridge, does work, and
emits proposals. It is the only layer that knows about *sequence*; it knows
nothing about who it works for.

## Hard rules

**1. Roles, never skills.** A node declares the role it needs (`plan`,
`review_charter`). The cartridge maps role → skill name. A graph that names a
skill has bound itself to one team's plugin layout.

**2. `args.cartridge` is required, with no fallback.** Not "defaults to the
last known config" — *required*, throw on absence. A fallback means the seam is
never exercised, so it rots silently while the cartridge drifts, and the first
symptom is a production run against year-old values.

**3. No domain constants.** Enforced, not requested — see
`tests/test_portability.py`. Tracker IDs, hosts, buckets, ARNs, account
numbers, usernames in paths: all read off the cartridge or absent.

**4. Scripts have no filesystem access and cannot read the clock.** Both are
arguments. `date` is passed in for the same reason the cartridge is: a graph
that reads the clock cannot be replayed, and a graph that cannot be replayed
cannot be debugged after the fact.

**5. Propose-only by default.** Nodes are read-only. Proposals are data. The
shell presents them at a gate and applies nothing without a decision. The one
allowed exception is a build node writing into a worktree it owns — nothing
pushed, nothing merged.

**6. Caps are explicit and overflow is visible.** Where a node bounds its work
(structured-output limits are real), the overflow defers to the next run and is
counted. Never silently truncate — a graph that drops nine of ten alerts and
reports success on the tenth is worse than one that fails.

## Node shape

Each node declares: the role it fills, the capability tier it wants (`cheap` /
`standard` / `deep` — resolved to a model by the provider profile), whether it
is read-only, and what it returns.

Return shapes stay small. A node that hands the next node a large blob has
moved the reasoning into the wrong place, and blows structured-output limits
on a busy day.

## Proposal shape

```
{ kind, risk, target, evidence[], rationale, suggested_action }
```

- `kind` must exist in the cartridge's `write_kinds`. Unknown kind → refuse.
- `evidence` is deterministic checks and their output, not prose. A claim
  without evidence is not a proposal, it is a guess with formatting.
- `risk` comes from the taxonomy, never invented at the node.

## Recording a run

Two calls, not a ritual: build the manifest (which hashes the resolved
cartridge), then record it — which derives ledger outcomes from the gate diffs
rather than trusting anything the run says about itself. Self-reported success
is exactly what the ledger exists to not believe.

## The graphs

| Graph | Shape |
|---|---|
| `lifecycle-propose` | plan → build (worktree) → review → emit. The dev loop. |
| `triage-propose` | fetch → classify against a runbook index → runbook-guided verify with deterministic checks → emit. Zero writes. |

Both are specified in `graphs/` and unimplemented — see the repo README for why.
