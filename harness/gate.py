"""The human gate, and the arms that execute what it (or the policy) cleared."""

from __future__ import annotations

import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from core.manifest import gate_diff

__all__ = ["APPLY_SCHEMA", "apply_arm_for", "apply_decisions", "auto_apply", "gate"]

# What an apply arm must report back. Small on purpose: the arm says whether it
# landed the write and names what it touched, and nothing else — a verbose arm
# is one that has started making decisions the gate already made.
APPLY_SCHEMA = {
    "type": "object",
    "properties": {
        "applied": {"type": "boolean"},
        "detail": {"type": "string"},
    },
    "required": ["applied", "detail"],
    "additionalProperties": False,
}


def apply_arm_for(kind: str, cartridge: dict[str, Any]) -> str | None:
    spec = (cartridge.get("write_kinds") or {}).get(kind)
    return spec.get("apply_arm") if isinstance(spec, Mapping) else None


def auto_apply(
    item: dict[str, Any],
    *,
    cartridge: dict[str, Any],
    runner: Any,
) -> tuple[bool, str]:
    """Execute a proposal the policy cleared, through the arm the cartridge names.

    An apply arm is a ROLE, so the same runner that ran the read-only nodes runs
    the write. `pr` has no executor here and is handed back to the gate rather
    than quietly reported as done.
    """
    arm = apply_arm_for(item["kind"], cartridge)
    if arm in (None, "pr"):
        return False, f"no executable apply arm for '{item['kind']}' (arm: {arm})"
    if arm == "shell":
        return False, "shell-armed kinds are applied by the run path that owns them"
    result = runner.run(
        role=arm,
        tier="standard",
        schema=APPLY_SCHEMA,
        context=list(cartridge.get("context") or []),
        # The rationale travels too: for an item_create it IS the body the arm
        # writes, and an arm handed only the action and the evidence was left
        # to refuse — correctly — for want of the content it was told to land.
        prompt=(
            f"Apply this approved proposal exactly as written. Do not widen it.\n\n"
            f"kind: {item['kind']}\ntarget: {item['target']}\n"
            f"action: {item['suggested_action']}\n"
            f"rationale (the content, where the action calls for one):\n{item['rationale']}\n\n"
            f"evidence: {item['evidence']}"
        ),
    )
    return bool(result.get("applied")), str(result.get("detail", ""))


def gate(proposals: list[dict[str, Any]], *, assume: str | None) -> tuple[list[dict[str, Any]], float]:
    """Present each proposal and capture a decision. Nothing applies without one.

    `human_minutes` is entered here, at the gate, not reconstructed later. A time
    saving recalled a month afterwards convinces nobody.
    """
    if not proposals:
        return [], 0.0

    decisions: list[tuple[dict[str, Any], str, bool]] = []
    started = datetime.now(UTC)

    for number, item in enumerate(proposals, 1):
        print(f"\n── proposal {number}/{len(proposals)} " + "─" * 44)
        print(f"  kind   : {item['kind']}  (risk: {item['risk']})")
        print(f"  target : {item['target']}")
        print(f"  action : {item['suggested_action']}")
        print(f"  why    : {item['rationale']}")
        print("  evidence:")
        for check in item["evidence"]:
            print(f"    - {check.get('check')}: {check.get('output')}")

        if assume:
            answer = assume
            print(f"  decision: {answer} (--assume)")
        else:
            answer = input("  [a]pprove / approve with [e]dits / [r]efuse ? ").strip().lower() or "r"

        decision, edited = {
            "a": ("approved", False),
            "e": ("approved", True),
            "r": ("refused", False),
        }.get(answer[:1], ("refused", False))
        decisions.append((item, decision, edited))

    minutes = (datetime.now(UTC) - started).total_seconds() / 60
    return decisions, round(minutes, 2)


def apply_decisions(
    decisions: list[tuple[dict[str, Any], str, bool]],
    *,
    cartridge: dict[str, Any],
    runner: Any,
) -> list[dict[str, Any]]:
    """Execute what the gate approved, then record what ACTUALLY happened.

    Approval is not execution. An earlier version passed `applied=decision ==
    "approved"` straight into `gate_diff`, so the ledger recorded `clean` for
    proposals nothing had ever run — a self-report, which is the one thing the
    ledger exists not to accept. `applied` comes from the arm returning
    successfully, and an approved proposal that could not be executed records
    `skipped`, which is exactly what it is.
    """
    diffs: list[dict[str, Any]] = []
    for item, decision, edited in decisions:
        applied = False
        if decision == "approved":
            applied, detail = auto_apply(item, cartridge=cartridge, runner=runner)
            if not applied:
                print(f"  approved but not executed ({detail})", file=sys.stderr)
        diffs.append(gate_diff(item, decision, applied=applied, edited=edited))
    return diffs
