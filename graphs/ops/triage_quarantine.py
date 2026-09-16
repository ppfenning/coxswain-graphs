"""triage-quarantine — facts and the cite check (docs/design/triage.md §1).

    facts -> triage -> emit

Only `facts` and the pure cite check land here; `triage` (the role call) and
`emit` are the next phase and land in this file afterward.

§1 names the field list and the input shape verbatim:

    `facts` (no model): from the work item and the task records of its
    attempts — quarantine reason and `kind` per attempt, review / adversary /
    arbitration verdicts, `fix_loop.stopped`, the trace's final result
    `subtype` per node, the evidence array, and whether the attempts predate
    the ticket's last revision (`attempts[].ts` vs the item file's last
    commit). Keyed facts: `attempt-<n>|<field>`.

    A cite naming a fact key not in `facts` is a fabricated citation and the
    answer is refused; zero cites is refused; a `diagnosis` that quotes no
    objection text from the record is refused.

`facts` is the harness's own arithmetic, not a model's recollection: it walks
the work item and the task records of its attempts and returns one flat
mapping keyed `attempt-<n>|<field>`, plain data in, plain data out, no clock,
no I/O, no network — the "item file's last commit" arrives as `work_item`'s
own `last_commit`, computed by the caller, never read from git here.

review, adversary and arbitration arrive shaped like a saved lifecycle
result — `{verdict, ...}` — read through `.get("verdict")`.

Per the chair's 2026-09-16 correction: a task record carries no per-node
trace subtype of its own: the trace lives beside the run, outside anything
this pure function is handed. Each attempt record MAY therefore carry an
optional `node_subtypes: {<node>: <subtype>}` mapping — assembled by the
caller (the epic, from the trace's own final result lines) and merely read
here. When an attempt carries it, `facts` emits one key per node,
`attempt-<n>|subtype:<node>`; when the attempt has no `node_subtypes` key (or
it is empty), `facts` emits no `subtype:` key for that attempt and nothing
else about the attempt changes.

The `triage` node that follows must cite exactly the keys `facts` produced; a
cite naming one it did not produce is a fabricated citation, and
`check_citation` is the mechanical check that catches it — alongside zero
cites and a diagnosis that quotes no objection text.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def _is_stale(ts: Any, last_commit: Any) -> bool:
    """`attempts[].ts` vs the item file's last commit, per §1."""
    if ts is None or last_commit is None:
        return False
    return ts < last_commit


def _attempt_facts(attempt: Mapping[str, Any], last_commit: Any) -> dict[str, Any]:
    base = {
        "reason": attempt.get("reason"),
        "kind": attempt.get("kind"),
        "review": (attempt.get("review") or {}).get("verdict"),
        "adversary": (attempt.get("adversary") or {}).get("verdict"),
        "arbitration": (attempt.get("arbitration") or {}).get("verdict"),
        "stopped": (attempt.get("fix_loop") or {}).get("stopped"),
        "evidence": attempt.get("evidence", ()),
        "stale": _is_stale(attempt.get("ts"), last_commit),
    }
    subtypes = attempt.get("node_subtypes") or {}
    return base | {f"subtype:{node}": subtype for node, subtype in subtypes.items()}


def facts(work_item: Mapping[str, Any], attempts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Keyed facts for `triage` to cite: `attempt-<n>|<field>`, 1-indexed.

    `attempt-<n>|subtype:<node>` appears only when that attempt's record
    carries `node_subtypes`; absent, not defaulted, when it does not.
    """
    last_commit = work_item.get("last_commit")
    return {
        f"attempt-{n}|{field}": value
        for n, attempt in enumerate(attempts, start=1)
        for field, value in _attempt_facts(attempt, last_commit).items()
    }


def check_citation(
    cites: Sequence[str],
    diagnosis: str,
    facts_: Mapping[str, Any],
    objections: Sequence[str],
) -> str | None:
    """None when the citation stands; otherwise the reason it is refused, per §1."""
    if not cites:
        return "zero cites: a triage answer must cite at least one fact"
    fabricated = [cite for cite in cites if cite not in facts_]
    if fabricated:
        return f"fabricated citation: {fabricated[0]!r} is not a key `facts` produced"
    if not any(objection and objection in diagnosis for objection in objections):
        return "diagnosis quotes no objection text verbatim from the record"
    return None
