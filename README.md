# agent-graphs

Portable agent graphs for the [`agent-cartridges`](https://github.com/ppfenning/agent-cartridges)
substrate. A graph owns *sequence*; a cartridge owns *who it works for*. The
seam between them is enforced by a test, not by good intentions.

```
graphs/   the graphs. pure functions of (args, runner)
runner/   node execution: the protocol, a scripted runner, the live one
shell.py  the only place with side effects — resolve, gate, apply, record
docs/     the contract every graph must satisfy
tests/    the portability check, and the graphs' own behaviour. run it in CI.
```

## Running one

```bash
pip install -e ".[dev]" && pip install -e ../agent-cartridges

python shell.py triage --team my-team \
  --alerts fixtures/alerts.json \
  --skills-root ~/repos/example-skills \
  --scripted fixtures/triage-run.json      # offline: canned nodes, no key
```

Drop `--scripted` and add `pip install -e ".[live]"` to run it against the real
API; the provider profile decides which model each tier means, and names the
env var holding the key.

## The shape of a graph

A graph is `run(args, runner) -> dict`. It owns sequence and nothing else:

- **Execution arrives as an argument.** `ScriptedRunner` replays canned dicts in
  tests, `AnthropicRunner` calls the Messages API in production, and neither the
  graph nor its tests change between them. That is why the whole suite runs in
  CI with no network and no key.
- **Nodes ask for a role and a tier**, never a skill and never a model. The
  cartridge maps role → skill; the provider profile maps tier → model.
- **Nothing writes.** The build node returns a unified diff; the *shell* applies
  it, inside a worktree it created, only after the gate approved it.
- **The policy runs before the gate.** `shell.py` asks `autonomy_policy` whether
  each kind has graduated, against the ledger filtered to this exact cartridge
  hash and provider profile. A graduated kind goes to its apply arm — itself a
  role — instead of the gate. An auto-applied proposal records **no ledger row**:
  autonomy is spent by acting and re-earned only at a gate, so a kind can never
  ratchet itself up on its own say-so.

## The graphs

| Graph | Shape |
|---|---|
| `initiative-decompose` | decompose → adversary-on-the-edges → emit. An idea into phases and a task DAG |
| `lifecycle-propose` | scope → plan → build (worktree) → handoff → review → adversary → arbitrate → emit |
| `triage-propose` | fetch → classify → verify → emit. Zero writes; proposes corrections to the runbook it just used |
| `epic-reconcile` | compare (set arithmetic) → reconcile → emit. Declared state vs actual |

## A phased build, with no tracker

```bash
python shell.py decompose --team local --idea "go arrow-native across the reader path"
python shell.py phase --team local --initiative work/arrow-migration --max-parallel 4
```

`decompose` turns the idea into phases and tasks and proposes each one; accepted
tasks land as markdown files under `work/`. `phase` reads that store, works out
which tasks are unblocked, and runs them **at the same time**, each in its own
worktree.

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

Verified against a deliberately-bad graph before shipping: **10 of the checks
fire** on a file carrying a tracker GID, an Atlassian host, a bucket URI, an
AWS ARN and account ID, a username-bearing path, an inline key, a silent
`args.cartridge` fallback, a defaulted cartridge, and a graph that accepts a
missing one. A check nobody has watched fail is not a check.

## Status: implemented

Both graphs are written and tested. They were written **fresh from the
specifications in `graphs/*.md` and the contract in `docs/`** — nothing was
ported from the implementations that exist from prior employment. See
[`agent-cartridges/docs/CLEAN-ROOM.md`](https://github.com/ppfenning/agent-cartridges/blob/main/docs/CLEAN-ROOM.md)
for the working rule and
[`PROVENANCE.md`](https://github.com/ppfenning/agent-cartridges/blob/main/docs/PROVENANCE.md)
for where the ideas came from.

Deferred from v0, and named in each graph's spec: the intake queue, epic-threshold
scoping, the adversarial reviewer pair, arbitration, the bounded fix loop, and
retro.

## Relationship to the other repos

| Repo | Owns |
|---|---|
| [`agent-cartridges`](https://github.com/ppfenning/agent-cartridges) | Substrate: cartridge merge, policy, manifest, ledger |
| **`agent-graphs`** | Sequence: what runs, in what order, at what tier |
| [`example-skills`](https://github.com/ppfenning/example-skills) | Craft: the skill bodies a cartridge binds roles to |

Split three ways because they change on three different clocks — the substrate
when the model of autonomy changes, graphs when a workflow changes, skills when
the craft improves. One caveat worth stating: graphs consume the cartridge
contract, so a breaking change there is a coordinated release across two repos.
Version the contract if that starts to hurt.
