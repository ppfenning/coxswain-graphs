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
  delivery/   work that produces work: initiative-decompose, lifecycle-propose,
              phase-validate (the epic driver's validators)
  ops/        keeping a running system honest: triage-propose, epic-reconcile,
              retro-propose, coxswain
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

This repository, `agent-cartridges`, and `agent-tools` are set up together.
The clone order, the environments, the logins, the provider profile, and the
verify step are written once, in
[`agent-tools/docs/getting-started.md`](https://github.com/ppfenning/agent-tools/blob/main/docs/getting-started.md),
which walks through all three from a clean machine.

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
| `lifecycle-propose` | delivery | scope → plan → [alternative plan → arbitrate plans] → [attack the plan] → build (worktree) → handoff → review → adversary → arbitrate → emit |
| `triage-propose` | ops | fetch → classify → verify → emit. Zero writes; proposes corrections to the runbook it just used |
| `epic-reconcile` | ops | compare (set arithmetic) → reconcile → emit. Declared state vs actual |
| `phase-validate` | delivery | validate_chunk per task → validate_phase against the phase's ORIGINAL goal. Invoked by the epic driver |
| `retro-propose` | ops | stats (pure arithmetic over ledger rows) → retro → emit. Proposes only what it can cite |
| `coxswain` | ops | one `dispatch` node over a driver-assembled docket. Selects; the harness invokes |

Each graph's diagram is generated from its own docstring by `python -m harness.graphdoc`; the committed pages live under [`docs/graphs/`](docs/graphs/index.md).

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

`epic` is the driver above `phase`: it walks the phase graph in dependency
order, stacks each phase's branch on its parent's head in the repository
`--repo` names, fans the lifecycle graph out per ready task, applies and
CHECKS every patch in a worktree it owns (`landing_areas.checks` — pass/fail
and parsed counts attach to the proposal as machine evidence), invokes the
`phase-validate` graph, and holds one gate per phase. Drafts land as local
branches, `merge_stack` joins them to the phase branch once earned or
approved, and nothing ever merges to the default branch — `merge_main` is
human-gated always, and the driver has no code path that emits it. Add
`--fix-attempts` to bound the lifecycle fix loop; a task that passes on a
retry carries its attempt count into the ledger, where a repeated-attempt
pass does not extend a streak. See `graphs/delivery/epic-swarm.md` for the
decided design, including what each comfort level means after the merge kind
split.

Solutions can compete before anyone builds. `lifecycle-propose` has three
optional roles between `plan` and `build`: `plan_alternative` writes a second
plan told to differ, `plan_arbitrate` picks one or merges them and names the
price, and `plan_adversary` attacks the winning plan's claims before a builder
spends a budget on it. Unbound means absent — a team that binds none keeps
the single-plan loop, and the competition itself needs both the alternative
and the arbiter, since an alternative nobody judges is a plan nobody builds.
A pick hands the source plan to the builder verbatim, so what was compared is
what gets built; only a merge is the arbiter's own plan. An objection the
adversary sustains buys ONE revision, routed back to whoever wrote the plan
on that seat's thread — the first planner, the second, or the arbiter when
the plan is a merge — and the revision is not attacked again. It sits before
build because a plan costs a fraction of a build, so two alternatives are
compared as two short documents rather than two diffs. The gap: there is no
tier gate yet. Once a cartridge binds these roles they run on every task,
whatever the change costs, so a one-line fix pays for a second plan, an
arbiter and an attack it did not need.

Five convictions hold this together:

- **Nothing is one-shot.** Every change gets a reviewer, and `review_tier`
  decides how many — a four-line migration is reviewed harder than a
  four-hundred-line rename, because scrutiny follows what a mistake would cost.
- **A step never builds on an unvalidated handoff.** The `handoff` role checks
  that what one step produced is what the next actually needs, and stops if it
  is not.
- **The dependency graph gets attacked.** An adversary reads the DAG looking for
  edges that are not real, because each one silently serialises work that could
  have run at once and nothing downstream will ever question it.
- **Claims are measured, not asked.** Diff shape is counted from the patch,
  check results come from running the project's own commands in a worktree the
  harness owns, and both attach to the proposal before the gate sees it.
- **The system cannot loosen its own rules.** A diff that touches governance —
  cartridges, skills, `core/policy.py`, `core/ledger.py`, `harness/`, the
  ledger file — is escalated to `self_modification` (`ramp: never`) from its
  paths alone, whatever kind the graph claimed. And the ledger itself lives
  OUTSIDE the working tree (`$XDG_STATE_HOME/agent-graphs/`), because a trust
  record you can reach through the thing it governs is not a record.

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
has watched fail is not a check. Re-verified 2026-09-04, with seven graphs
and three drivers in the tree: the same planted file still lights up the same
**9 checks**, and the suite passes again once it is removed.

## Status: implemented

The graphs and the harness are written and tested. They were written **fresh
from the specifications in `graphs/*/**.md` and the contract in `docs/`** —
nothing was ported from the implementations that exist from prior employment.
See
[`agent-cartridges/docs/CLEAN-ROOM.md`](https://github.com/ppfenning/agent-cartridges/blob/main/docs/CLEAN-ROOM.md)
for the working rule and
[`PROVENANCE.md`](https://github.com/ppfenning/agent-cartridges/blob/main/docs/PROVENANCE.md)
for where the ideas came from.

Everything deferred from v0 has since landed: the intake queue (a directory
the coxswain drains, `agent-cartridges/core/intake.py`), the bounded fix
loop (in `lifecycle-propose`, with its attempt count carried to the ledger),
retro (`retro-propose`, which proposes only what it can cite), and the
coxswain dispatcher — a one-node graph that selects from the registry,
with a driver (`harness/cos.py`) that invokes the selection through the same
nested-invocation primitive the epic driver fans out with. What remains
deferred is named where it is deferred: comfort level 2 (`pr_ready_flip`
stays gated in base, and nothing yet justifies loosening it for everyone) and
a forge arm, so a "draft PR" is a local branch until one exists.

### What changed (2026-09-04), and why

Three fixes from live runs, and one new shape. The chunk validator was
refusing tasks both reviewers had approved, saying no diff was provided —
it had been handed the description, the evidence rows, the change facts and
the review verdict, and never the patch; the diff git applied is machine
evidence, not the builder's account of it, so it now joins what validators
see, and the handoff's `ContractViolation` names the work item's id again
instead of embedding its whole text where the id belongs (#6). A patch that
arrives through a JSON field can lose its final newline, and git then rejects
it as corrupt at its last line although every hunk is intact — two
gate-approved patches were lost that way in one epic, so the worktree restores
the newline before `git apply` (#7). And the lifecycle graph grew the plan
competition described above — `plan_alternative`, `plan_arbitrate`,
`plan_adversary` — with the arbiter's `plan` field required only on a merge,
because a plan written alongside a pick is a plan nobody reads, and a merge
that claims the name without the plan stops the graph rather than quietly
building the first plan under it (#8).

### What changed (2026-09-01, second pass), and why

The system stopped being read-only. The check arm runs the project's own
tests against the applied patch and attaches the result as evidence; the epic
driver runs a whole initiative to a stack of branches while landing nothing
on main; `merge` split by target so within-initiative stack merges became
earnable without loosening anything; the fix loop retries with the critique
attached but records its attempts, and the policy refuses to let a third-try
pass extend a streak; triage's `trap_held` verdict now demotes the exact
runbook entry that was wrong, because trust is earned per entry, not per
kind; and the harness escalates any diff that touches governance to a kind
no streak can graduate. Autonomy became buyable only where the asymmetry was
measured — and unbuyable where the system would be grading itself.

### What changed (2026-09-01, first pass), and why

`shell.py` grew into a 400-line script that owned every side effect and named
every graph. The machinery moved into `harness/` with a public API, graphs now
register via `SPEC` instead of being enumerated, and the graphs themselves are
namespaced by function. The rename is not cosmetic: "graph harness" used to
conflate the program with the runtime, and the split is what a coxswain
graph — a graph whose nodes dispatch other graphs — needs to exist without
being a special case.

## Relationship to the other repos

| Repo | Owns |
|---|---|
| [`agent-cartridges`](https://github.com/ppfenning/agent-cartridges) | Substrate: cartridge merge, policy, manifest, ledger — and the reference `local-skills` plugin |
| **`agent-graphs`** | Harness (the runtime) + graphs (the programs) |
| a skills plugin | Craft: the skill bodies a cartridge binds roles to — `local-skills` ships with the substrate; teams point `--skills-root` at their own |

## License

MIT — see [LICENSE](LICENSE).
