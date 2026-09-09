"""What a run spent, per node — written beside the manifest, never guessed.

A runner that can count (the headless Claude Code runner records cost and
tokens per call) exposes `.calls`; this writes them next to the run's manifest
and prints one honest line. A runner that cannot count records nothing, and
the absence is visible rather than papered over with an estimate.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

__all__ = ["record_usage", "summarize"]


def summarize(calls: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Pure: totals and a per-model breakdown over the recorded calls."""
    fields = ("input_tokens", "cache_read_tokens", "cache_creation_tokens", "output_tokens")

    def total(call: Mapping[str, Any]) -> int:
        # Older records carried only a summed `input_tokens`; newer ones split it.
        return int(call.get("input_total") if call.get("input_total") is not None else call.get("input_tokens") or 0)

    by_model: dict[str, dict[str, Any]] = {}
    for call in calls:
        row = by_model.setdefault(str(call.get("model")), {"calls": 0, "cost_usd": 0.0, "input_total": 0, **dict.fromkeys(fields, 0)})
        row["calls"] += 1
        row["cost_usd"] = round(row["cost_usd"] + float(call.get("cost_usd") or 0.0), 4)
        row["input_total"] += total(call)
        for f in fields:
            row[f] += int(call.get(f) or 0)
    return {
        "calls": len(calls),
        "cost_usd": round(sum(float(c.get("cost_usd") or 0.0) for c in calls), 4),
        "turns": sum(int(c.get("turns") or 0) for c in calls),
        "input_total": sum(total(c) for c in calls),
        **{f: sum(int(c.get(f) or 0) for c in calls) for f in fields},
        "by_model": by_model,
    }


def _read_ledger(path: Path) -> list[dict[str, Any]]:
    """Every line of the per-call ledger that parses as a JSON object; a bad line is skipped, not fatal."""
    if not path.exists():
        return []
    rows = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def record_usage(runner: Any, *, runs_dir: Path | str, run_id: str) -> dict[str, Any] | None:
    """Write `<runs_dir>/<run_id>.usage.json` from the per-call ledger, unioned with `runner.calls` by
    `(role, trace)` with the ledger winning a shared key. Returns the summary."""
    ledger_calls = _read_ledger(Path(runs_dir) / f"{run_id}.calls.jsonl")
    seen = {(row.get("role"), row.get("trace")) for row in ledger_calls}
    extra = [c for c in (getattr(runner, "calls", None) or []) if (c.get("role"), c.get("trace")) not in seen]
    calls = ledger_calls + extra
    if not calls:
        return None
    summary = summarize(calls)
    path = Path(runs_dir) / f"{run_id}.usage.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"run_id": run_id, "summary": summary, "calls": list(calls)}, indent=2), encoding="utf-8")
    models = ", ".join(f"{m}: {r['calls']} call(s) ${r['cost_usd']}" for m, r in summary["by_model"].items())
    cached = summary["cache_read_tokens"]
    share = f", {100 * cached // summary['input_total']}% of input was cache reads" if summary["input_total"] else ""
    print(f"  usage   : {summary['calls']} node call(s), {summary['turns']} turns, ${summary['cost_usd']} — {models}{share} → {path}")
    return summary
