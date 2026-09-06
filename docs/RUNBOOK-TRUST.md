# Trust is earned per runbook entry

**Status:** implemented (2026-09-01). `agent-cartridges/core/policy.py` counts
streaks per `subject` with a kind-level fallback and gates `subject_new`
unconditionally; ledger rows carry the optional `subject`; triage's `doc_update`
proposals name their entry; and the harness files a `failure` observation
against an entry whose trap did not hold (`harness/cli.py`), so demotion no
longer waits for a human to refuse something. The open questions below were
resolved as: the incident signal enters as an argument-shaped observation from
the harness after the run; blast radius is narrow (only the implicated entry);
an amendment does not yet reset the entry's streak (deferred — it needs entry
content hashing, and a wrong amendment still demotes on its next failure); the
schema grew the nullable `subject` exactly as proposed; and the field was not
generalised beyond graphs that have one.

## The gap

Today `autonomy_policy` measures a `(kind, risk)` pair, scoped to a
`cartridge_sha` and a `provider_profile`. For triage that grain is too coarse in
both directions.

`doc_update` is one kind. But a runbook index is not one thing — it is forty
entries of wildly different quality. An entry that has classified the same
symptom correctly thirty times and an entry written yesterday from a single
sighting are, to the policy, indistinguishable: they graduate together, on a
streak that is the *average* of every entry's behaviour. That average hides
exactly the case worth catching. A kind can be riding a clean streak while one
of its entries is quietly wrong every time it fires, because the other
thirty-nine carry it.

And it fails the other way too. One bad entry poisons the streak for a kind that
is otherwise trustworthy, so good entries are held back by a bad neighbour they
have nothing to do with.

## The change

**The runbook entry is the principal.** Ledger rows from triage carry the entry
they were produced under — `rb-04` — and streaks are counted per entry, not
just per kind. `SCOPE_KEYS` grows a third dimension for graphs that have one.

This does not contradict rule 6 ("the principal is the GRAPH, never a person").
It sharpens it. The thing being trusted was never really the *category* of
write; it was the encoded judgment that produced it. In triage, that judgment
lives in the runbook entry — its `match`, its checks, and its `trap`. Trusting
`doc_update` in the abstract is trusting a container. Trusting `rb-04` is
trusting a claim that can actually be wrong.

## Incidents demote

An entry loses standing on any of three signals. The first already exists; the
other two are new:

| Signal | Meaning | Source |
|---|---|---|
| Reversal | a human edited or refused the proposal | the gate, as today |
| `trap_held: false` | the entry's stated wrong belief was itself wrong | `verify` already reports this |
| **An incident implicating the entry** | someone followed the runbook and it went wrong | post-hoc, after the run |

The second is the interesting one, because `verify` already reports it and today
it only produces a `doc_update` proposal. An entry whose trap did not hold has
been demonstrated to point at the wrong belief — that should reset its streak,
not merely suggest an edit.

The third is the strongest reversal available and rule 3 already contemplates
its shape ("a post-hoc detector fired"). An incident is the runbook being
*wrong in production*, which is the only evidence that actually settles the
question. It arrives late, after the row was written and possibly after the
write auto-applied. That is fine: the ledger is append-only, and a later row
demoting an entry is exactly how a track record should work.

## The asymmetry is the safety property

An entry earns trust slowly — `graduation_n` consecutive clean outcomes — and
loses it on a single incident, with `regraduation_multiplier` raising the bar it
must clear to come back. Same shape as rule 3, applied at the grain where the
judgment actually lives. Cheap to earn back a little, expensive to earn back
after being wrong twice.

## Open questions

- **Where does the incident signal enter?** Triage takes alerts as an argument
  precisely so the graph never reads a queue. An incident feed has the same
  constraint and probably the same answer: an argument, not a fetch.
- **Blast radius.** Does an incident demote only the implicated entry, or its
  neighbours in the same symptom family? Narrow is more honest; wide is safer.
  Probably narrow, on the grounds that a policy that punishes uninvolved entries
  is one nobody will trust enough to leave switched on.
- **Does an amendment reset the streak?** Likely yes — an amended entry is a new
  entry, and the track record belonged to the text that was replaced. This
  mirrors the cartridge-hash rule: a record earned under different rules is not
  a record.
- **Schema.** Rows need a `subject` (nullable). Graphs without one behave
  exactly as today, so this is additive.
- **Generality.** Triage has runbook entries; `lifecycle-propose` has none, or
  has something else. Worth resisting the urge to generalise the field before a
  second graph actually needs it.

## The project overlay layer

**Status:** implemented (2026-09-06). A fourth cartridge layer, read from the
TARGET repository rather than from `agent-cartridges` itself:
`<repo>/.agent/cartridge.yaml`. It has no `extends` — it is the last word, not
another link in the chain — and it merges LAST, on top of whatever the
profile's team cartridge already resolved to. Absent, the resolved cartridge
is byte-identical to a run without one.

Only four keys are read out of it:

- `context` — concatenates onto the context list, as every layer does.
- `policy.review_tier` — tighten only. An overlay may add a tier2 surface or
  lower a size threshold; it may not raise one or remove a surface the team
  already set.
- `landing_areas.checks` — the run's checks. This subsumes `.agent-checks`:
  that file is a shorthand that populates this key only when the overlay
  itself does not set it.
- `description`.

Every other top-level key is refused. `skills`, `cast`, `write_kinds`,
`extends`, and anything else the overlay declares raises `CartridgeError`
naming the offending key — an overlay tightens a team's policy, it does not
hand the target repository a second cast or a second set of write kinds.

The overlay's own text is folded into `cartridge_sha`, so a run under an
overlay and a run without one are provably different runs. The resolved
cartridge also carries `overlay_sha`: the sha256 of the overlay text when
`<repo>/.agent/cartridge.yaml` was present, `None` when it was not.

`agent-graphs` writes `overlay_sha` beside `cartridge_sha` wherever it builds
a per-run record from the resolved cartridge itself: today that is the
entry-level observation `_observe_trap_failures` files when a `doc_update`
trap did not hold (`harness/cli.py`). The phase manifest that `build_manifest`
and `record_run` produce for every run does not yet carry `overlay_sha` — that
function lives in `agent-cartridges`, and its current signature neither
accepts the field as an argument nor reads it off the `cartridge` mapping it
is already handed, confirmed by calling it directly with an `overlay_sha`-
bearing cartridge and reading back its keys. Passing `cartridge` unchanged
picks up `overlay_sha` automatically once `build_manifest` reads it the way it
already reads `cartridge_sha`; landing that read is a change to
`agent-cartridges`, not to `agent-graphs`.
