# agent-graphs

Portable agent graphs, and the **harness** that runs them against the
[`agent-cartridges`](https://github.com/ppfenning/agent-cartridges) substrate.

Four nouns, one seam each:

| Noun | Owns | Lives in |
|---|---|---|
| **Harness** | consequences: side effects, policy, the gate, the ledger | `harness/` |
| **Graph** | sequence — what runs, in what order, at what tier. Writes nothing | `graphs/` |
| **Cartridge** | who a run works for: role → skill, where writes land | `agent-cartridges` |
| **Runner** | execution: canned dicts in tests, the Messages API live | `runner/` |

```
harness/   the runtime. resolve, discover, run, gate, apply, record
graphs/    the programs. pure functions of (args, runner)
  delivery/   work that produces work: initiative-decompose, lifecycle-propose
  ops/        keeping a running system honest: triage-propose, epic-reconcile
runner/    node execution: the protocol, a scripted runner, the live one
shell.py   two-line compatibility shim; `python shell.py ...` still works
docs/      the contract every graph must satisfy
tests/     the portability check, and the graphs' own behaviour. run it in CI.
```

## Running one

```bash
git clone https://github.com/ppfenning/agent-cartridges ../agent-cartridges
pip install -e ".[dev]" && pip install -e ../agent-cartridges

python shell.py triage --team local \
  --alerts fixtures/alerts.json \
  --skills-root ../agent-cartridges/skills-plugins \
  --scripted fixtures/triage-run.json      # offline: canned nodes, no key
```

The `local` cartridge and the skills it binds ship together in
`agent-cartridges`, so that command resolves from a clean clone. The gate is
interactive; add `--assume r` (or `a`) to answer it non-interactively. Drop
`--scripted` and add `pip install -e ".[live]"` to run against the real API;
the provider profile decides which model each tier means, and names the env
var holding the key.

## The shape of a graph

A graph is `run(args, runner) -> dict`. It owns sequence and nothing else:

- **Execution arrives as an argument.** `ScriptedRunner` replays canned dicts in
  tests, `AnthropicRunner` calls the Messages API in production, and neither the
  graph nor its tests change between them. That is why the whole suite runs in
  CI with no network and no key.
- **Nodes ask for a role and a tier**, never a skill and never a model. The
  cartridge maps role → skill; the provider profile maps tier → model. The
  harness resolves the bound skill's body and the live runner prepends it to
  that node's system prompt — a binding is load-bearing, not decorative.
- **Nothing writes.** The build node returns a unified diff; the *harness*
  applies it, inside a worktree it created, only after the gate approved it.
- **The policy runs before the gate.** The harness asks `autonomy_policy`
  whether each kind has graduated, against the ledger filtered to this exact
  cartridge hash and provider profile. A graduated kind goes to its apply arm —
  itself a role — instead of the gate. An auto-applied proposal records **no
  ledger row**: autonomy is spent by acting and re-earned only at a gate, so a
  kind can never ratchet itself up on its own say-so.
- **A graph registers itself.** Each module declares a `SPEC` — its subcommand,
  its entrypoint, and its inputs as declarative `Need`s — and the harness
  discovers it. Adding a graph to the CLI is dropping a module into `graphs/`,
  not editing a dispatch table. The spec never performs I/O; the harness reads
  the files the needs name, which is how the graph side stays pure enough for
  the portability suite to hold it to that.

## The graphs

| Graph | Namespace | Shape |
|---|---|---|
| `initiative-decompose` | delivery | decompose → adversary-on-the-edges → emit. An idea into phases and a task DAG |
| `lifecycle-propose` | delivery | scope → plan → build (worktree) → handoff → review → adversary → arbitrate → emit |
| `triage-propose` | ops | fetch → classify → verify → emit. Zero writes; proposes corrections to the runbook it just used |
| `epic-reconcile` | ops | compare (set arithmetic) → reconcile → emit. Declared state vs actual |

## A phased build, with no tracker

```bash
python shell.py decompose --team local --idea "go arrow-native across the reader path" \
  --skills-root ../agent-cartridges/skills-plugins \
  --scripted fixtures/decompose-run.json --assume r

python shell.py phase --team local --initiative fixtures/work/example-initiative \
  --skills-root ../agent-cartridges/skills-plugins \
  --scripted fixtures/phase-run.json --assume r --max-parallel 4
```

`decompose` turns an idea into phases and tasks and proposes each one; accepted
tasks land as markdown files under `work/` (live runs land them through the
work-item arm — the scripted fixture only replays the nodes). `phase` reads a
work store, works out which tasks are unblocked, and runs the lifecycle graph
over them **at the same time**, each in its own worktree.
`fixtures/work/example-initiative/` is a committed three-task store so the
second command has something real to schedule: two ready tasks run in
parallel, and the third stays blocked because its dependency edges are real.

Three convictions hold this together:

- **Nothing is one-shot.** Every change gets a reviewer, and `review_tier`
  decides how many — a four-line migration is reviewed harder than a
  four-hundred-line rename, because scrutiny follows what a mistake would cost.
- **A step never builds on an unvalidated handoff.** The `handoff` role checks
  that what one step produced is what the next actually needs, and stops if it
  is not.
- **The dependency graph gets attacked.** An adversary reads the DAG looking for
  edges that are not real, because each one silently serialises work that could
  have run at once and nothing downstream will ever question it.

There is no ticketing platform anywhere in this. `cartridges/local/` binds every
role to the filesystem: work items are markdown files, git is the audit trail,
and the cartridge has no tracker, no workspace id, and no `auth_env`.

## The portability check

```bash
pytest tests/test_portability.py -q
```

It fails the build if a graph inlines a tracker ID, host, bucket, ARN, account
number, credential, or a path containing a username — and if a graph reads
`args.cartridge` without throwing when it is absent. Patterns, not a denylist
of names: a denylist only catches the employer you remembered to add.

Three checks police the cartridge seam specifically: the text scan for an
inlined fallback, a Python-specific one for `args.get("cartridge", <default>)`
— the fallback that does not look like one at a glance — and a behavioural one
that imports every graph and asserts it *refuses* to run without a cartridge.
The last is the one that matters: a grep-only check passes happily on a graph
that never mentions the word.

Verified against a deliberately-bad graph — and re-verified after the graphs
moved into namespaces, because a check that sweeps the wrong directory passes
by finding nothing. A file planted in `graphs/ops/` carrying a tracker GID, an
Atlassian host, a bucket URI, an AWS ARN and account ID, a username-bearing
path, an inline key, a silent `args.cartridge` fallback, and a missing-cartridge
acceptance lit up **9 checks**, including the behavioural refusal — which
proves the module walk actually descends into the new layout. A check nobody
has watched fail is not a check.

## Status: implemented

The graphs and the harness are written and tested. They were written **fresh
from the specifications in `graphs/*/**.md` and the contract in `docs/`** —
nothing was ported from the implementations that exist from prior employment.
See
[`agent-cartridges/docs/CLEAN-ROOM.md`](https://github.com/ppfenning/agent-cartridges/blob/main/docs/CLEAN-ROOM.md)
for the working rule and
[`PROVENANCE.md`](https://github.com/ppfenning/agent-cartridges/blob/main/docs/PROVENANCE.md)
for where the ideas came from.

Deferred from v0, and named in each graph's spec: the intake queue, the bounded
fix loop, retro — and a **chief-of-staff dispatch graph**, which now has what it
needs (a registry to select from, and a phase driver to fan out with) and is
the next graph to write.

### What changed (2026-09-01), and why

`shell.py` grew into a 400-line script that owned every side effect and named
every graph. The machinery moved into `harness/` with a public API, graphs now
register via `SPEC` instead of being enumerated, and the graphs themselves are
namespaced by function. The rename is not cosmetic: "graph harness" used to
conflate the program with the runtime, and the split is what a chief-of-staff
graph — a graph whose nodes dispatch other graphs — needs to exist without
being a special case.

## Relationship to the other repos

| Repo | Owns |
|---|---|
| [`agent-cartridges`](https://github.com/ppfenning/agent-cartridges) | Substrate: cartridge merge, policy, manifest, ledger — and the reference `local-skills` plugin |
| **`agent-graphs`** | Harness (the runtime) + graphs (the programs) |
| a skills plugin | Craft: the skill bodies a cartridge binds roles to — `local-skills` ships with the substrate; teams point `--skills-root` at their own |
