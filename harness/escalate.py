"""Escalation: a change to the rules is not whatever kind the graph called it.

A system that can propose changes to its own policy, its own ledger, its own
cartridges, or its own harness can — on an earned streak in some mundane kind —
auto-apply a change that loosens its own rules. Nothing upstream stops that. The
graph names the kind, and a `draft_pr_create` that happens to edit
`core/policy.py` is still a `draft_pr_create`; the policy then reads a clean
streak and clears it to write. The write is the rule change.

So the harness decides instead, from the diff's paths alone: if a patch touches
governance, every patch-bearing proposal in that run is rewritten to the
`self_modification` kind, whatever kind the graph claimed. Same discipline as
`_change_facts` in `graphs/delivery/lifecycle_propose.py` and as
`harness/checks.py` — derived from the artifact, never from a model, and never
from the run's own account of itself.

Two properties make this hold rather than merely exist:

**It runs AFTER graph emission and BEFORE the policy split.** Not inside a
graph, which would make the defence something a graph could forget to call, and
not after `split_by_policy`, which would be a verdict on a decision already
made. Between the two is the only place where no streak on any mundane kind can
carry a governance edit past the gate.

**`self_modification` is `ramp: never`.** The policy returns PROPOSE for it
unconditionally, so there is nothing a graph can claim about itself that gets
the change back down to a kind that auto-applies. Escalation is one-way by
construction, not by our remembering to make it so.

The parser fails open on purpose. A path this module misses is a path that does
not gate, so every ambiguity here resolves toward over-collection: extra paths
cost a line of console output, a missed one costs the whole argument above.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

__all__ = [
    "GOVERNANCE_PATHS",
    "PATCH_KINDS",
    "SELF_MODIFICATION",
    "escalate_self_modification",
    "governance_hits",
    "touched_paths",
]

# What "its own rules" means, spelled out as paths. The cartridges are the
# configuration a run is judged against, the skills-plugins are the craft it is
# judged by, policy.py is the judgment itself, ledger.py is the record the
# judgment reads, and harness/ is every hand that touches any of them —
# including this file.
GOVERNANCE_PATHS: tuple[str, ...] = (
    "cartridges/",
    "skills-plugins/",
    "core/policy.py",
    "core/ledger.py",
    "harness/",
)

# The kinds that carry the patch. These are what a governance edit actually
# rides in on; a scoping `item_create` or a `comment_add` emitted by the same
# run is not the change and is not rewritten.
PATCH_KINDS: frozenset[str] = frozenset(
    {"draft_pr_create", "merge_stack", "merge_main", "stack_rebase", "pr_ready_flip"}
)

SELF_MODIFICATION = "self_modification"

_DEV_NULL = "/dev/null"


def _strip_prefix(path: str) -> str:
    """Drop git's `a/` or `b/` prefix, and any leading `./` or `/`.

    Under `--no-prefix` a real directory named `a` loses a segment here. That
    is survivable precisely because `governance_hits` matches on any segment
    boundary rather than the string start: the truncated path still hits the
    same entry the full one would.
    """
    path = path.strip()
    for prefix in ("a/", "b/"):
        if path.startswith(prefix):
            path = path[len(prefix) :]
            break
    while path.startswith("./"):
        path = path[2:]
    return path.lstrip("/")


def _header_path(line: str) -> str | None:
    """The path off a `---`/`+++` header, or None for /dev/null and junk."""
    raw = line.split("\t", 1)[0][4:].strip()
    if not raw or raw == _DEV_NULL:
        return None
    return _strip_prefix(raw) or None


def _git_header_paths(line: str) -> list[str]:
    """Both sides of a `diff --git a/<x> b/<y>` line.

    Read because a pure rename and a mode-only change produce this line and no
    `---`/`+++` pair at all — a diff whose only content is "harness/gate.py is
    now executable" would otherwise register as touching nothing.
    """
    rest = line[len("diff --git ") :].strip()
    marker = rest.find(" b/")
    if rest.startswith("a/") and marker != -1:
        # Split on the ` b/` seam rather than on whitespace, so a path with a
        # space in it survives intact.
        return [_strip_prefix(rest[:marker]), _strip_prefix(rest[marker + 1 :])]
    return [_strip_prefix(token) for token in rest.split()]


def touched_paths(patch: str) -> list[str]:
    """Every path a unified diff mentions, deduplicated and sorted.

    Mechanical: the `---`/`+++` headers and the `diff --git` line, nothing
    inferred and nothing asked of anyone. `/dev/null` is not a path, so an add
    reports only its `b/` side and a delete only its `a/` side.
    """
    found: set[str] = set()
    for line in (patch or "").splitlines():
        if line.startswith("diff --git "):
            found.update(p for p in _git_header_paths(line) if p and p != _DEV_NULL)
        elif line.startswith("--- ") or line.startswith("+++ "):
            path = _header_path(line)
            if path:
                found.add(path)
    return sorted(found)


def _segments_match(path_segments: Sequence[str], entry_segments: Sequence[str]) -> bool:
    """Does the entry appear as a run of whole segments anywhere in the path?

    Whole segments, so `harnessy/file.py` does not hit `harness/`. Anywhere,
    so a diff rooted a level up — `repo/harness/gate.py`, which is exactly what
    a patch built against a parent checkout looks like — still does.
    """
    span = len(entry_segments)
    return any(
        list(path_segments[i : i + span]) == list(entry_segments)
        for i in range(len(path_segments) - span + 1)
    )


def governance_hits(
    paths: Iterable[str],
    *,
    ledger_path: Path | str,
    extra: Iterable[str] = (),
) -> list[str]:
    """Which of these paths touch the rules. Sorted, deduplicated, and specific.

    The returned paths are the evidence — a proposal escalated with "governance
    paths touched" and no paths would be an assertion, and this system does not
    accept those from anyone, including itself.

    The ledger is matched on **basename**, wherever it sits: it is configurable
    (`--ledger`), so its path is not a constant this module could list. A patch
    writing any file by that name is either editing the trust record or
    shadowing it with one of its own, and both are the same act as far as the
    gate is concerned.
    """
    entries = [tuple(e.rstrip("/").split("/")) for e in (*GOVERNANCE_PATHS, *extra) if e.strip("/")]
    ledger_name = Path(ledger_path).name

    hits: set[str] = set()
    for path in paths:
        if not path:
            continue
        segments = path.split("/")
        if ledger_name and segments[-1] == ledger_name:
            hits.add(path)
            continue
        if any(_segments_match(segments, entry) for entry in entries):
            hits.add(path)
    return sorted(hits)


def escalate_self_modification(
    proposals: Sequence[Mapping[str, Any]],
    *,
    patch: str,
    cartridge: Mapping[str, Any],
    ledger_path: Path | str,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Rewrite this run's patch-bearing proposals if the patch touches the rules.

    Returns the proposals to carry forward and the governance paths that were
    hit. A clean patch returns the proposals unchanged and no hits — this must
    be free when it is not needed, or it becomes something to route around.

    A cartridge that does not declare `self_modification` raises. It is
    tempting to synthesise the kind, or to gate on some nearby high-risk kind
    instead, and both would be worse than the hole they patch: `proposal()`
    refuses kinds the taxonomy does not authorise, and a taxonomy that cannot
    name this kind cannot express the `ramp: never` that makes the escalation
    mean anything. A cartridge that cannot name the kind cannot gate it, and
    silence here is the exact hole this module closes.
    """
    hits = governance_hits(touched_paths(patch), ledger_path=ledger_path)
    if not hits:
        return list(proposals), []

    spec = (cartridge.get("write_kinds") or {}).get(SELF_MODIFICATION)
    if not isinstance(spec, Mapping):
        raise ValueError(
            f"patch touches governance ({', '.join(hits)}) but cartridge "
            f"'{cartridge.get('team', '?')}' declares no '{SELF_MODIFICATION}' write kind; "
            "a cartridge that cannot name the kind cannot gate it"
        )
    risk = spec.get("risk")
    if not risk:
        raise ValueError(
            f"cartridge '{cartridge.get('team', '?')}' declares '{SELF_MODIFICATION}' "
            "with no risk; risk comes from the taxonomy and is never invented here"
        )

    row = {"check": "governance_paths", "output": ", ".join(hits)}
    escalated: list[dict[str, Any]] = []
    for item in proposals:
        if item.get("kind") not in PATCH_KINDS:
            escalated.append(dict(item))
            continue
        escalated.append(
            {
                **item,
                "kind": SELF_MODIFICATION,
                "risk": risk,
                "escalated_from": item.get("kind"),
                # Extended, never replaced. The check evidence and the review's
                # findings are still true about this change; what changed is
                # what the change is allowed to do without a human.
                "evidence": [*(item.get("evidence") or []), row],
            }
        )
    return escalated, hits
