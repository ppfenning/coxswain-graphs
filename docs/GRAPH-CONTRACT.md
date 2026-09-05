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

## The shell's duties, in order

Three, and the first one is easy to forget because the contract used only ever
spelled out the last two:

**1. Before the gate — measure, then escalate, then ask the policy.** When
`--repo` names the project a change targets, the harness applies the build
patch in a real worktree of it and RUNS the cartridge's configured checks
(`landing_areas.checks`), attaching pass/fail and parsed counts to the proposal
the way `change_facts` already work — counted from reality, never asked of a
model. Then, from the diff's paths alone, it escalates any patch-bearing
proposal whose change touches governance (`cartridges/`, `skills-plugins/`,
`core/policy.py`, `core/ledger.py`, `harness/`, the ledger file) to the
`self_modification` kind, `ramp: never`, whatever kind the graph claimed — a
system must not loosen its own rules on its own say-so. Only then: For every proposal, consult
`autonomy_policy` against the ledger, filtered to this exact `cartridge_sha` and
provider profile. Without this the gate asks about every kind forever, no streak
is ever spent, and earned autonomy is decoration. *This clause exists because the
first implementation shipped `policy.py` fully unit-tested and never called it —
a pure function cannot notice a caller that never calls it.*

An auto-applied proposal gets **no ledger row**. The ledger records what happened
at a gate, and an auto-apply never reached one. If acting on a streak could
extend that streak, a kind would ratchet itself up forever on its own say-so,
which is the self-report the ledger exists to disbelieve. Autonomy is spent by
acting, re-earned only at the gate, and lost when a detector files an
observation.

An apply arm is a **role**, so the same runner that ran the read-only nodes runs
the write. An arm with no executor here (`pr`) goes to the gate rather than being
reported as done.

**2 and 3. After the run — two calls, not a ritual:** build the manifest (which
hashes the resolved cartridge), then record it — which derives ledger outcomes
from the gate diffs rather than trusting anything the run says about itself.

## Review is proportional to cost, and nothing is one-shot

Every change gets a reviewer. How many it gets is decided by `review_tier` in
the cartridge, not by the author:

| Tier | When | Reviewers |
|---|---|---|
| 0 | matches `tier0_patterns` | charter |
| 1 | within `tier1_max_changed_lines` / `tier1_max_modules` | charter + adversary |
| 2 | touches a `tier2_surfaces` surface, or is simply big | charter + adversary + arbitration, **whether or not they disagreed** |

The dangerous-surface check runs first and cannot be talked down by size: a
four-line migration outranks a four-hundred-line rename. Tier 0 is the cheapest
review, never the absence of one. At tier 2, two reviewers agreeing is not by
itself evidence — which is why arbitration runs anyway.

Reviewer roles are optional. A team that binds none of them gets a single
reviewer, and an unbound adversary is not a silent objection.

## Steps hand off, and a handoff is checked

Between two steps sits a `handoff` node: does what the last step produced
actually contain what the next one needs? If not, the graph **stops**. A
reviewer handed half a change produces a confident opinion about the wrong
thing, and a phase that goes quietly wrong usually did so several steps earlier.

Its second job is compression — the next step gets a small brief rather than
everything that came before, which is the existing rule about return shapes
staying small, enforced at the seam where it actually matters.

## The graphs

| Graph | Shape |
|---|---|
| `initiative-decompose` | decompose → adversary-on-the-edges → emit. Idea into phases and a task DAG. |
| `lifecycle-propose` | scope → plan → [alternative plan → arbitrate plans] → [attack the plan] → build (worktree) → handoff → review → adversary → arbitrate → emit. One task. |
| `triage-propose` | fetch → classify → verify → emit. Zero writes; proposes its own runbook corrections. |
| `epic-reconcile` | compare (set arithmetic) → reconcile → emit. Declared state vs actual. |
| `phase-validate` | validate_chunk per task → validate_phase against the phase's ORIGINAL goal. Invoked by the epic driver. |
| `retro-propose` | stats (pure arithmetic over ledger rows) → retro → emit. Proposes only what it can cite. |
| `coxswain` | one `dispatch` node over a driver-assembled docket. Selects; the driver invokes. |

All seven are specified in `graphs/` and implemented.

Running a *phase* is not a graph: the shell runs `lifecycle-propose` once per
unblocked task, concurrently. Sequence belongs to a graph; concurrency belongs
to the I/O edge that already owns every side effect. Results are ordered by task
id before anything is recorded, so wall-clock order never reaches the ledger.
The same ruling, made once in `harness/invoke.py`, governs everything that
blocks on futures: the phase driver, the epic driver (`harness/epic.py`), and
the coxswain driver (`harness/cos.py`). None of them carries a `SPEC`,
and every child run records under the parent's run id.

## None of this needs a tracker

The roles and write kinds name work, not vendors: `work_state_arm`,
`work_item_arm`, `item_create`, `state_move`. `cartridges/local/` binds every one
of them to the filesystem — work items are markdown files under `work/`, git is
the audit trail — and resolves with no tracker, no workspace id, and no
`auth_env`. If a graph needed a tracker, that cartridge could not resolve.
