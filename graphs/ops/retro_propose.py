"""retro-propose — the ledger reads back. Only proposes what it can cite.

    stats -> retro -> emit

A retro over a running system is not a model asked "how did we do" from
memory — memory is exactly what the ledger exists to disbelieve. So the stats
this graph reasons about are computed HERE, mechanically, as pure dict
arithmetic over the rows the harness read: per `(kind, subject)` bucket, the
outcome counts, the current consecutive-clean streak (a clean that took more
than one build attempt is transparent, same rule the policy itself uses so a
struggling loop cannot manufacture the record it stands on), the reversal
count, and how many cleans were not first-try.

The single node then reasons only about numbers that are already established
fact, the same argument `epic-reconcile`'s `compare` node makes for set
arithmetic. Its answer is graded against those facts on the way out: every
observation and proposal must CITE the bucket key(s) it rests on, and a cite
naming a bucket the stats do not contain is refused as a fabricated citation.
The model cites; the graph substantiates the evidence from the stats it cited.
That is retro's whole discipline, and it is enforced, not requested.

`doc_update` is the only kind this graph emits in v1. `charter_proposal` and
`skill_proposal` — the write kinds a retro naturally wants to reach for when
the finding is "the charter itself is wrong" or "a skill needs a rewrite" —
declare no `risk` in the base taxonomy (see cartridges/base/cartridge.yaml),
and `graphs._contract.proposal` REFUSES to emit any kind without one. Rather
than inventing a risk at the node — which is exactly the move the contract
exists to forbid — those findings come back as `observations`: data, cited the
same way a proposal is, but not routed through the gate. That is a scope limit
of this graph, not a design choice worth being quiet about; see
`retro-propose.md`.

Zero ledger rows is a legitimate answer, not an error: a retro over nothing
has nothing to learn, and asking a model to comment on an empty ledger is
ceremony, so no node call happens at all.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from graphs._contract import ContractViolation, proposal, require, require_cartridge
from runner.protocol import NodeRunner

__all__ = ["GRAPH_NAME", "run"]

GRAPH_NAME = "retro-propose"

DEFAULT_MAX_PROPOSALS = 5

# Outcomes that end a streak. Mirrors `core.policy.STREAK_BREAKING`: a reversal
# or a post-hoc failure both say the human, or a later check, did not accept
# what was proposed.
_STREAK_BREAKING = frozenset({"reversal", "failure"})
_OUTCOME_KEYS = ("clean", "reversal", "skipped", "failure")

RETRO_SCHEMA = {
    "type": "object",
    "properties": {
        "observations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "about": {"type": "string"},
                    "detail": {"type": "string"},
                    "cites": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["about", "detail", "cites"],
                "additionalProperties": False,
            },
        },
        "proposals": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "target": {"type": "string"},
                    "rationale": {"type": "string"},
                    "suggested_action": {"type": "string"},
                    "cites": {"type": "array", "items": {"type": "string"}},
                    "subject": {"type": "string", "description": "empty string means no subject"},
                },
                "required": ["target", "rationale", "suggested_action", "cites", "subject"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["observations", "proposals"],
    "additionalProperties": False,
}


def _bucket_key(kind: str, subject: str | None) -> str:
    """`"<kind>|<subject-or-->"` — the citation vocabulary the model must use."""
    return f"{kind}|{subject or '-'}"


def _first_try(row: Mapping[str, Any]) -> bool:
    return int(row.get("attempts", 1) or 1) <= 1


def _compute_stats(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Bucket the ledger by (kind, subject) and compute what a retro may cite.

    Walks each bucket in the rows' own relative order (the ledger is
    append-only and read oldest-first, so that order is already chronological)
    — the same walk `core.policy._streak_and_bar` does, so a retro's idea of
    "the current streak" never disagrees with the policy's.
    """
    order: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        kind = str(row.get("kind"))
        raw_subject = row.get("subject")
        subject = str(raw_subject) if raw_subject not in (None, "") else None
        order.setdefault(_bucket_key(kind, subject), []).append(row)

    buckets: dict[str, dict[str, Any]] = {}
    for key, bucket_rows in order.items():
        kind, _, subject_part = key.partition("|")
        counts = dict.fromkeys(_OUTCOME_KEYS, 0)
        streak = 0
        attempts_gt1_clean = 0
        for row in bucket_rows:
            outcome = row.get("outcome")
            if outcome in counts:
                counts[outcome] += 1
            if outcome in _STREAK_BREAKING:
                streak = 0
            elif outcome == "clean":
                if _first_try(row):
                    streak += 1
                else:
                    attempts_gt1_clean += 1
            # `skipped`, and a clean beyond first try, are transparent: they
            # neither prove the bucket trustworthy nor prove it wrong.
        buckets[key] = {
            "kind": kind,
            "subject": None if subject_part == "-" else subject_part,
            "counts": counts,
            "streak": streak,
            "reversal_count": counts["reversal"],
            "attempts_gt1_clean": attempts_gt1_clean,
        }

    overall_clean = sum(b["counts"]["clean"] for b in buckets.values())
    overall_reversal = sum(b["counts"]["reversal"] for b in buckets.values())
    agreement = overall_clean / (overall_clean + overall_reversal) if (overall_clean + overall_reversal) else None

    return {"buckets": buckets, "overall": {"rows": len(rows), "agreement": agreement}}


def _evidence_output(bucket: Mapping[str, Any]) -> str:
    counts = bucket["counts"]
    parts = [f"{counts['reversal']} reversal", f"{counts['clean']} clean"]
    if counts["skipped"]:
        parts.append(f"{counts['skipped']} skipped")
    if counts["failure"]:
        parts.append(f"{counts['failure']} failure")
    parts.append(f"streak {bucket['streak']}")
    return ", ".join(parts)


def _validate_cites(cites: Sequence[str], buckets: Mapping[str, Any], *, label: str) -> None:
    for cite in cites:
        if cite not in buckets:
            raise ContractViolation(
                f"{label} cites bucket '{cite}', which the stats do not contain — a fabricated citation"
            )


def run(args: Mapping[str, Any], runner: NodeRunner) -> dict[str, Any]:
    """Run the graph. The ledger's rows arrive as an argument, already parsed."""
    cartridge = require_cartridge(args)
    run_id, date = require(args, "run_id", "date")

    rows = args.get("ledger_rows")
    if rows is None:
        raise ContractViolation(
            "args.ledger_rows is required. This graph does not read the ledger itself — "
            "a graph must not read its own trust record."
        )
    rows = list(rows)

    max_proposals = int(args.get("max_proposals") or DEFAULT_MAX_PROPOSALS)

    if not rows:
        # A retro over nothing has nothing to learn; a node call here would be
        # ceremony, not judgment.
        return {
            "run_id": run_id,
            "date": date,
            "observations": [],
            "proposals": [],
            "totals": {"rows": 0, "buckets": 0, "proposals": 0, "deferred_overflow": 0},
        }

    bound = cartridge.get("skills") or {}
    if "retro" not in bound:
        raise ContractViolation(
            "this graph needs the optional role 'retro' bound in the cartridge; "
            "a team that has not bound it cannot run a retro"
        )

    stats = _compute_stats(rows)
    buckets = stats["buckets"]
    context = list(cartridge.get("context") or [])

    retro = dict(
        runner.run(
            role="retro",
            tier="deep",
            schema=RETRO_SCHEMA,
            context=context,
            prompt=(
                f"Review this ledger.\n\nDate: {date}\nRows: {len(rows)}\n\n"
                f"Per-bucket stats, computed mechanically — a bucket key is "
                f"'kind|subject-or--':\n{buckets}\n\nOverall: {stats['overall']}\n\n"
                "Only claim what these numbers show. Every observation and every proposal "
                "must cite the bucket key(s) that support it in `cites`; a claim citing "
                "nothing is not a finding. Propose `doc_update` fixes only — anything you "
                "would otherwise propose as a charter or skill change belongs in "
                "`observations`, with `about` naming what needs to change, until the "
                "taxonomy carries a risk for those kinds. Use `subject` when the proposal "
                "is about one entry rather than the whole kind; leave it empty otherwise."
            ),
        )
    )

    observations_raw = list(retro.get("observations") or [])
    for observation in observations_raw:
        _validate_cites(
            observation.get("cites") or [],
            buckets,
            label=f"observation about '{observation.get('about')}'",
        )
    observations = [
        {"about": str(o.get("about", "")), "detail": str(o.get("detail", "")), "cites": list(o.get("cites") or [])}
        for o in observations_raw
    ]

    validated: list[dict[str, Any]] = []
    for raw in retro.get("proposals") or []:
        target = str(raw.get("target", ""))
        cites = list(raw.get("cites") or [])
        if not cites:
            raise ContractViolation(
                f"retro proposal for '{target}' carries no cites; retro's whole discipline "
                "is that claims cite rows"
            )
        _validate_cites(cites, buckets, label=f"proposal for '{target}'")
        subject = str(raw.get("subject") or "").strip() or None
        evidence = [{"check": f"ledger:{cite}", "output": _evidence_output(buckets[cite])} for cite in cites]
        validated.append(
            {
                "target": target,
                "subject": subject,
                "evidence": evidence,
                "rationale": str(raw.get("rationale", "")),
                "suggested_action": str(raw.get("suggested_action", "")),
            }
        )

    proposals: list[dict[str, Any]] = []
    overflow = 0
    for item in validated:
        if len(proposals) >= max_proposals:
            overflow += 1
            continue
        proposals.append(
            proposal(
                cartridge,
                kind="doc_update",
                target=item["target"],
                subject=item["subject"],
                evidence=item["evidence"],
                rationale=item["rationale"],
                suggested_action=item["suggested_action"],
            )
        )

    return {
        "run_id": run_id,
        "date": date,
        "observations": observations,
        "proposals": proposals,
        "totals": {
            "rows": len(rows),
            "buckets": len(buckets),
            "proposals": len(proposals),
            "deferred_overflow": overflow,
        },
    }


from graphs._spec import GraphSpec, Need  # noqa: E402

SPEC = GraphSpec(
    name="retro",
    graph_name=GRAPH_NAME,
    run=run,
    summary="reads the ledger's own rows and proposes only doc_update fixes it can cite",
    needs=(
        Need(
            "ledger_rows",
            flag="--ledger-rows",
            kind="jsonl_file",
            help="path to a ledger the harness reads; the graph gets the parsed rows — a "
            "graph must not read its own trust record",
        ),
        Need(
            "max_proposals",
            flag="--max-proposals",
            kind="int",
            required=False,
            help="cap on doc_update proposals per run (default 5); overflow is counted, never dropped",
        ),
    ),
)
