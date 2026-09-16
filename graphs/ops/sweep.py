"""sweep — one rule applied in many places: plan, apply, verify, review.

sweep_plan -> sweep_apply -> sweep_verify -> [review pair]

The complement to `decompose`: work that needs no per-unit design, only one
judgment call applied everywhere it matches. `sweep_plan` reads the whole
repository through its own tool loop and writes the map, the exceptions and
the application; this module never touches a filesystem itself outside
`sweep_apply`, which is the edge that runs the script or the one build call.
`sweep_verify` takes what `sweep_apply` already captured — no model, no I/O of
its own — and the review pair on the one resulting diff is `review-diff`,
the same pair every other graph here uses, not a copy of it.

See docs/design/work-shape.md §1.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

from graphs._contract import require, require_cartridge
from graphs.delivery import review_entry
from graphs.delivery.lifecycle_propose import BUILD_SCHEMA
from harness.checks import all_passed, checks_evidence, run_checks
from harness.worktree import apply_patch, create_worktree
from runner.protocol import NodeRunner

__all__ = ["GRAPH_NAME", "run", "sweep_apply", "sweep_plan", "sweep_verify"]

GRAPH_NAME = "sweep"

SWEEP_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "map": {"type": "array", "items": {"type": "object"}},
        "exceptions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "reason": {"type": "string"}},
                "required": ["path", "reason"],
                "additionalProperties": False,
            },
        },
        "application": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["script", "build"]},
                "body": {"type": "string"},
            },
            "required": ["kind", "body"],
            "additionalProperties": False,
        },
        "postcondition": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["map", "exceptions", "application", "postcondition"],
    "additionalProperties": False,
}


def sweep_plan(cartridge: Mapping[str, Any], runner: NodeRunner, idea: str) -> dict[str, Any]:
    """The one deep-tier call: map, exceptions, application and postcondition, per §1."""
    return dict(
        runner.run(
            role="sweep_plan",
            tier="deep",
            schema=SWEEP_PLAN_SCHEMA,
            context=list(cartridge.get("context") or []),
            prompt=(
                "One rule applied in many places. Read the whole repository and write the MAP "
                "of old to new (or the rule as a function of the match), the EXCEPTIONS with a "
                "reason each, the APPLICATION as a script or, only where the rule cannot be "
                "written, one build brief, and the POSTCONDITION as grep patterns that must "
                f"return zero matches.\n\nIdea:\n{idea}"
            ),
        )
    )


def _grep_matches(worktree: Path, pattern: str, exceptions: Sequence[Mapping[str, Any]]) -> list[str]:
    """Every `path:line` under `worktree` matching `pattern`, outside the exceptions' files."""
    rx = re.compile(pattern)
    excepted = {str(item.get("path")) for item in exceptions}
    hits: list[str] = []
    for path in sorted(worktree.rglob("*")):
        if not path.is_file() or ".git" in path.parts:
            continue
        rel = str(path.relative_to(worktree))
        if rel in excepted:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        hits.extend(f"{rel}:{lineno}" for lineno, line in enumerate(text.splitlines(), start=1) if rx.search(line))
    return hits


def sweep_apply(cartridge: Mapping[str, Any], runner: NodeRunner, repo: str, plan: Mapping[str, Any]) -> dict[str, Any]:
    """Run the plan's application in a fresh worktree: the edge. A script takes no model."""
    application = plan.get("application") or {}
    kind, body = application.get("kind"), str(application.get("body") or "")
    with TemporaryDirectory() as scratch:
        worktree = Path(scratch) / "worktree"
        ok, worktree_detail = create_worktree(Path(repo), worktree, branch=f"sweep-{uuid4().hex[:8]}")
        if not ok:
            return {"applied": False, "diff": "", "error": worktree_detail}

        if kind == "script":
            proc = subprocess.run(["sh", "-c", body], cwd=worktree, capture_output=True, text=True)
            if proc.returncode != 0:
                return {"applied": False, "diff": "", "error": (proc.stderr or proc.stdout).strip()}
        elif kind == "build":
            build = dict(
                runner.run(
                    role="sweep_build",
                    tier="standard",
                    schema=BUILD_SCHEMA,
                    context=list(cartridge.get("context") or []),
                    prompt=f"Apply this map exactly; touch nothing outside it.\n\nMap:\n{plan.get('map')}\n\n{body}",
                )
            )
            applied, apply_detail = apply_patch(build.get("patch", ""), worktree)
            if not applied:
                return {"applied": False, "diff": "", "error": apply_detail}
        else:
            return {"applied": False, "diff": "", "error": f"unknown application.kind {kind!r}"}

        diff = subprocess.run(["git", "diff"], cwd=worktree, capture_output=True, text=True).stdout
        postcondition = [
            {"pattern": pattern, "matches": _grep_matches(worktree, pattern, plan.get("exceptions") or [])}
            for pattern in plan.get("postcondition") or []
        ]
        checks_config = (cartridge.get("landing_areas") or {}).get("checks") or []
        results = run_checks(worktree, checks_config)
        return {
            "applied": True,
            "diff": diff,
            "postcondition": postcondition,
            "checks_passed": all_passed(results),
            "checks": checks_evidence(results),
        }


def sweep_verify(applied: Mapping[str, Any]) -> dict[str, Any]:
    """No model: every postcondition pattern must have greped to zero, and checks pass.

    Takes what `sweep_apply` already captured, as plain data; no I/O of its own.
    """
    if not applied.get("applied"):
        return {"verdict": "fail", "failures": [str(applied.get("error") or "the application did not apply")]}

    failures = [
        f"{row['pattern']}: {', '.join(row['matches'][:3])}"
        for row in applied.get("postcondition") or []
        if row.get("matches")
    ]
    if not applied.get("checks_passed", True):
        failures.append("configured checks did not pass")
    return {"verdict": "fail", "failures": failures} if failures else {"verdict": "pass", "failures": []}


def run(args: Mapping[str, Any], runner: NodeRunner) -> dict[str, Any]:
    """sweep_plan -> sweep_apply -> sweep_verify -> the ordinary review pair, over one diff."""
    cartridge = require_cartridge(args)
    run_id, date, repo, idea = require(args, "run_id", "date", "repo", "idea")

    plan = sweep_plan(cartridge, runner, str(idea))
    applied = sweep_apply(cartridge, runner, str(repo), plan)
    verify = sweep_verify(applied)

    if verify["verdict"] != "pass":
        return {"run_id": run_id, "date": date, "plan": plan, "verify": verify, "review": None}

    review = review_entry.run(
        {"cartridge": cartridge, "diff": applied["diff"], "repo": repo, "ref": args.get("ref") or "HEAD"}, runner
    )
    return {"run_id": run_id, "date": date, "plan": plan, "verify": verify, "review": review}


from graphs._spec import GraphSpec, Need  # noqa: E402

SPEC = GraphSpec(
    name="sweep",
    graph_name=GRAPH_NAME,
    run=run,
    summary="one rule applied everywhere it matches: plan, apply in a fresh worktree, verify by grep, then review",
    needs=(
        Need("repo", flag="--target-repo", help="the repository the sweep runs over"),
        Need("idea", flag="--idea", kind="text_or_path", help="the rule to apply, or a path to it"),
        Need(
            "ref",
            flag="--ref",
            required=False,
            help="the ref review-diff checks the resulting diff against (default HEAD)",
        ),
    ),
)
