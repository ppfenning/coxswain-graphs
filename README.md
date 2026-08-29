# agent-graphs

Portable agent graphs for the [`agent-cartridges`](https://github.com/ppfenning/agent-cartridges)
substrate. A graph owns *sequence*; a cartridge owns *who it works for*. The
seam between them is enforced by a test, not by good intentions.

```
graphs/   specifications (implementations pending — see Status)
docs/     the contract every graph must satisfy
tests/    the portability check. run it in CI.
```

## The portability check

```bash
pytest tests/test_portability.py -q
```

It fails the build if a graph inlines a tracker ID, host, bucket, ARN, account
number, credential, or a path containing a username — and if a graph reads
`args.cartridge` without throwing when it is absent. Patterns, not a denylist
of names: a denylist only catches the employer you remembered to add.

Verified against a deliberately-bad graph before shipping: 6 of the checks fire
on a file carrying a tracker GID, an Atlassian host, a bucket URI, a
username-bearing path, an inline key, and a silent `args.cartridge` fallback.
A check nobody has watched fail is not a check.

## Status: specified, not implemented

The two graphs here are specifications. Working implementations exist from
prior employment and are **deliberately not ported**.

They carry near-zero employer coupling — one of them passes this very
portability check as written — but coupling and ownership are different
questions. The graphs were conceived during employment as part of a design
that is under an open invention-assignment question; a clean-room rewrite
resolves the expression, not the assignment. See
[`agent-cartridges/docs/PROVENANCE.md`](https://github.com/ppfenning/agent-cartridges/blob/main/docs/PROVENANCE.md)
for the full reasoning and the confirmation request that is outstanding.

So: contracts now, implementations written fresh once that lands. The
portability check is new work and runs today.

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
