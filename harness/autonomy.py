"""The policy consultation: which proposals have earned the right to skip the gate."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from core import ledger
from core.policy import AUTO, autonomy_policy

__all__ = ["split_by_policy"]


def split_by_policy(
    proposals: list[dict[str, Any]],
    *,
    cartridge: dict[str, Any],
    ledger_path: Path | str,
    provider_profile: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Ask the policy which proposals have earned the right to skip the gate.

    THE CALLER FILTERS. `autonomy_policy` refuses rows spanning more than one
    configuration rather than averaging across them, so the filter here is not a
    nicety — it is the precondition that makes the question answerable at all.

    Note what an auto-applied proposal does NOT get: a ledger row. The ledger
    records what happened at the gate, and an auto-apply never reached one. If
    acting on a streak could extend that streak, a kind would ratchet itself up
    forever on its own say-so, which is the exact self-report the ledger exists
    to disbelieve. Autonomy is spent by acting, and only re-earned at the gate —
    or lost when a detector files an observation against it.

    THE POLICY IS ASKED AT THE GRAIN THE PROPOSAL NAMES. A proposal that carries
    a `subject` — the runbook entry a `doc_update` amends, say — has its streak
    read over that entry's own rows and no others. The entry, not the category,
    is what earned or lost the trust: `doc_update` is a container, and forty
    good entries averaging over one that is wrong every time it fires is exactly
    the case the ledger exists to surface. A proposal carrying `subject_new`
    creates its subject, so it can never ride the kind's streak — there is no
    track record for something that does not exist yet, and creation is the
    moment a wrong entry is cheapest to catch.

    Absence is passed through as absence. A proposal with no subject gets the
    kind-level reading, which counts every row whatever subject it carries — the
    strict direction, and the only one that keeps the fallback honest. Writing a
    default subject here would invent a track record; the policy would then be
    answering a question nobody asked.

    Caps stay keyed per kind. `applied_this_run` bounds how much of a kind may
    land in one run, which is a question about blast radius, not about which
    entry earned what.
    """
    rows = [
        row
        for row in ledger.read(ledger_path)
        if row.get("cartridge_sha") == cartridge.get("cartridge_sha")
        and row.get("provider_profile") == provider_profile
    ]
    base = {**(cartridge.get("policy") or {}), "write_kinds": cartridge.get("write_kinds") or {}}

    auto: list[dict[str, Any]] = []
    gated: list[dict[str, Any]] = []
    applied_so_far: Counter[str] = Counter()

    for item in proposals:
        config = {
            **base,
            "applied_this_run": applied_so_far[item["kind"]],
            # Forwarded only when the proposal actually carries them; the policy
            # reads both, and an invented value there is an invented streak.
            **({"subject": item["subject"]} if "subject" in item else {}),
            **({"subject_new": item["subject_new"]} if "subject_new" in item else {}),
        }
        if autonomy_policy(item["kind"], item["risk"], rows, config) == AUTO:
            auto.append(item)
            applied_so_far[item["kind"]] += 1
        else:
            gated.append(item)
    return auto, gated
