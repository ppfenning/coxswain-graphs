"""Reuse what an earlier run already earned, so a fix does not cost the whole epic again.

Five epics ran against the same initiative while the runner was being
corrected. Each redid every task from scratch, including the ones whose
lifecycle had already produced an approved patch, and that rework was most of
the bill. A task's lifecycle result is a pure function of its inputs plus the
model's answers — once it has an approved patch, running it again buys a
second opinion at full price.

So every task's lifecycle result is saved beside the manifest, and a later run
may name an earlier one to resume from: a task whose saved result carries an
approved patch is reused and costs nothing; everything else runs. Reuse is by
task id within the same initiative — it says nothing about whether the patch
still APPLIES to a phase branch that may have moved, and it does not need to:
the harness applies and checks every reused patch exactly as it would a fresh
one, so a stale patch quarantines with git's own diagnosis rather than
slipping through.

What makes a result reusable is deliberately narrow and read off the record,
never inferred: the graph emits a `draft_pr_create` proposal only on an
approved verdict, and a patch has to exist to be applied.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

__all__ = ["load_result", "result_path", "reusable", "save_result"]


def result_path(runs_dir: Path | str, run_id: str, phase: str, task: str) -> Path:
    """`<runs_dir>/<run_id>/tasks/<phase>/<task>.json` — one file per task per run."""
    return Path(runs_dir) / run_id / "tasks" / phase / f"{task}.json"


def reusable(result: Mapping[str, Any] | None) -> bool:
    """Pure: an approved lifecycle result — a `draft_pr_create` proposal — with a patch to apply."""
    if not isinstance(result, Mapping):
        return False
    patch = str((result.get("build") or {}).get("patch") or "")
    approved = any(
        isinstance(p, Mapping) and p.get("kind") == "draft_pr_create" for p in result.get("proposals") or []
    )
    return approved and bool(patch.strip()) and not (result.get("fix_loop") or {}).get("stopped")


def save_result(result: Mapping[str, Any], *, runs_dir: Path | str, run_id: str, phase: str, task: str) -> Path:
    path = result_path(runs_dir, run_id, phase, task)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(result), indent=2, default=str), encoding="utf-8")
    return path


def load_result(runs_dir: Path | str, run_id: str, phase: str, task: str) -> dict[str, Any] | None:
    """The saved result, or None when there is none or it cannot be read."""
    path = result_path(runs_dir, run_id, phase, task)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None
