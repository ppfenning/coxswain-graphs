"""review-diff — the charter reviewer, the adversary, verify-evidence and arbitration on an arbitrary diff.

review_charter -> review_adversary -> verify_evidence -> [arbitrate]

Takes a diff, a repo and a ref — never a run's own build branch — and returns
the same verdict-and-findings shape a build's own review step produces.
Nothing is written to the repo and nothing is pushed.

Unlike every other graph here, this one touches a real worktree directly
rather than asking the runner for one: its whole point is to check a repo and
ref that are not this run's own state, so there is no upstream build step to
have already created it. verify_evidence runs only when the cartridge
configures checks; otherwise there is nothing to check and none of this runs.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

from graphs._contract import require, require_cartridge
from graphs._spec import GraphSpec, Need
from graphs.delivery.lifecycle_propose import (
    ADVERSARY_SCHEMA,
    ARBITRATE_SCHEMA,
    REVIEW_SCHEMA,
    _abstained_adversary,
    _abstained_review,
    _reviewer_answer,
)
from harness.checks import all_passed, checks_evidence, run_checks
from harness.worktree import apply_patch, create_worktree
from runner.protocol import NodeRunner

__all__ = ["GRAPH_NAME", "SPEC", "review_request", "run"]

GRAPH_NAME = "review-diff"


def review_request(diff: str, repo: str, ref: str) -> dict[str, Any]:
    """The review inputs a build's own review step gets, shaped for a standalone diff."""
    return {"diff": diff, "repo": repo, "ref": ref}


def _finding(item: Mapping[str, Any]) -> dict[str, Any]:
    """One posted finding, its line pulled off a `path:line` file cite when the reviewer gave one."""
    raw_file = str(item.get("file") or "")
    path, sep, tail = raw_file.rpartition(":")
    file, line = (path, int(tail)) if sep and tail.isdigit() else (raw_file, 0)
    return {
        "file": file,
        "line": line,
        "detail": str(item.get("detail") or ""),
        "charter_principle": str(item.get("charter_principle") or ""),
    }


def _verify_evidence(
    repo: str, ref: str, diff: str, checks_config: Sequence[Mapping[str, Any]]
) -> tuple[bool, list[dict[str, str]]]:
    """Apply `diff` in a fresh worktree of `ref` and run the repo's own configured checks."""
    if not checks_config:
        return True, []
    with TemporaryDirectory() as scratch:
        worktree = Path(scratch) / "worktree"
        ok, detail = create_worktree(Path(repo), worktree, branch=f"review-diff-{uuid4().hex[:8]}", base=ref)
        if not ok:
            return False, [{"check": "checks:worktree", "output": f"FAIL — {detail}"}]
        applied, detail = apply_patch(diff, worktree)
        if not applied:
            return False, [{"check": "checks:apply", "output": f"FAIL — {detail}"}]
        results = run_checks(worktree, checks_config)
        return all_passed(results), checks_evidence(results)


def run(args: Mapping[str, Any], runner: NodeRunner) -> dict[str, Any]:
    """Run the graph: charter, adversary, verify-evidence, and arbitration on disagreement."""
    cartridge = require_cartridge(args)
    diff, repo, ref = require(args, "diff", "repo", "ref")
    context = list(cartridge.get("context") or [])

    charter, charter_abstained = _reviewer_answer(
        runner,
        role="review_charter",
        model_tier="standard",
        schema=REVIEW_SCHEMA,
        context=context,
        prompt=(
            "Review this change against the team's own written charter in your context.\n\n"
            f"Patch:\n{diff}\n\n"
            "Cite the charter principle behind every finding, and give each finding's "
            "file as `path:line`."
        ),
    )
    if charter_abstained:
        charter = _abstained_review()

    adversary, adversary_abstained = _reviewer_answer(
        runner,
        role="review_adversary",
        model_tier="standard",
        schema=ADVERSARY_SCHEMA,
        context=context,
        prompt=(
            "Your job is to disagree. Find what this change gets wrong, and what the "
            "first reviewer accepted too easily.\n\n"
            f"First reviewer said: {charter.get('verdict')} — {charter.get('rationale')}\n"
            f"Patch:\n{diff}\n\n"
            "State your strongest objection plainly, even if you end up approving."
        ),
    )
    if adversary_abstained:
        adversary = _abstained_adversary()

    checks_config = (cartridge.get("landing_areas") or {}).get("checks") or []
    checks_passed, evidence = _verify_evidence(repo, ref, diff, checks_config)

    disagreed = not charter_abstained and not adversary_abstained and charter.get("verdict") != adversary.get("verdict")
    arbitration: dict[str, Any] | None = None
    if disagreed:
        arbitration = dict(
            runner.run(
                role="arbitrate",
                tier="deep",
                schema=ARBITRATE_SCHEMA,
                context=context,
                prompt=(
                    "Two reviewers have looked at this change. Decide.\n\n"
                    f"Charter reviewer: {charter.get('verdict')} — {charter.get('rationale')}\n"
                    f"Adversary: {adversary.get('verdict')} — {adversary.get('strongest_objection')}\n"
                    f"Verify-evidence: {'passed' if checks_passed else 'failed'}\n\n"
                    "Say who you sided with and why. 'neither' is allowed."
                ),
            )
        )

    if arbitration is not None:
        verdict, rationale = str(arbitration.get("verdict")), str(arbitration.get("reasoning"))
    else:
        verdict = "approve" if charter.get("verdict") == adversary.get("verdict") == "approve" else "revise"
        rationale = str(charter.get("rationale") or "")
    verdict = verdict if checks_passed else "revise"

    return {
        "verdict": verdict,
        "findings": [_finding(item) for item in charter.get("findings") or []],
        "rationale": rationale,
        "checks": evidence,
    }


SPEC = GraphSpec(
    name="review-diff",
    graph_name=GRAPH_NAME,
    run=run,
    summary="review an arbitrary diff outside any build's own run — verdict and findings out, nothing written",
    needs=(
        Need("diff", flag="--diff", kind="text_or_path", help="the diff to review, or a path to it"),
        Need("repo", flag="--target-repo", help="the repository the diff targets"),
        Need("ref", flag="--ref", help="the ref verify_evidence checks out a fresh worktree from"),
    ),
)
