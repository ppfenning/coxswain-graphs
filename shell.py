"""The shell: resolve a cartridge, run a graph, gate its proposals, record it.

This is the only place the three repos meet. It imports the substrate
(`agent-cartridges`) for resolution and recording, imports a graph for sequence,
and owns every side effect between them — the worktree, the patch application,
the prompt at the gate, the files written afterwards.

Graphs stay pure because this file is not. That is the trade, and it is
deliberate: everything worth unit-testing lives on the other side of it.

    python shell.py lifecycle --team acme --ticket TICKET-1 \\
        --skills-root ~/repos/example-skills --scripted fixtures/run.json

Shell duty after a run is two calls — build_manifest, then record_run — and they
are made here so nobody has to remember them.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import uuid
from datetime import date as date_type
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.cartridge import CartridgeError, load
from core.manifest import build_manifest, gate_diff, record_run
from core.skills import index_from_roots

from graphs import lifecycle_propose, triage_propose
from graphs._contract import ContractViolation
from runner import ScriptedRunner
from runner.protocol import RunnerError

GRAPHS = {"lifecycle": lifecycle_propose, "triage": triage_propose}

REPO_ROOT = Path(__file__).resolve().parent


def _resolve_cartridge(args: argparse.Namespace) -> dict[str, Any]:
    index = index_from_roots(args.skills_root)
    if args.unverified_skills:
        print("warning: skill bindings NOT verified (--unverified-skills)", file=sys.stderr)

        class _Unverified(dict):
            def get(self, key, default=None):
                return [key]

        index = _Unverified()
    return load(args.team, args.cartridges_dir, skill_index=index)


def _build_runner(args: argparse.Namespace, cartridge: dict[str, Any]):
    if args.scripted:
        responses = json.loads(Path(args.scripted).read_text(encoding="utf-8"))
        return ScriptedRunner(responses)

    from runner.anthropic_runner import AnthropicRunner, load_provider_profile

    return AnthropicRunner(load_provider_profile(args.provider_profile))


def _apply_patch(patch: str, worktree: Path) -> tuple[bool, str]:
    """Apply the build node's diff inside the worktree the shell owns.

    The one place anything in this system writes to a working tree, and it is
    the shell doing it — not the node that produced the patch. An agent that
    owns a worktree can be wrong destructively without costing anything.
    """
    worktree.mkdir(parents=True, exist_ok=True)
    # git apply needs a work tree. The shell makes one it owns rather than
    # applying anywhere near a real checkout — an agent that owns a worktree can
    # be wrong destructively without costing anything.
    if not (worktree / ".git").exists():
        subprocess.run(["git", "init", "-q"], cwd=worktree, capture_output=True, text=True)
    result = subprocess.run(
        ["git", "apply", "--verbose", "--allow-empty", "-"],
        input=patch,
        cwd=worktree,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0, (result.stderr or result.stdout).strip()


def _gate(proposals: list[dict[str, Any]], *, assume: str | None) -> tuple[list[dict[str, Any]], float]:
    """Present each proposal and capture a decision. Nothing applies without one.

    `human_minutes` is entered here, at the gate, not reconstructed later. A time
    saving recalled a month afterwards convinces nobody.
    """
    if not proposals:
        return [], 0.0

    diffs: list[dict[str, Any]] = []
    started = datetime.now(timezone.utc)

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
        diffs.append(gate_diff(item, decision, applied=decision == "approved", edited=edited))

    minutes = (datetime.now(timezone.utc) - started).total_seconds() / 60
    return diffs, round(minutes, 2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="shell.py", description=__doc__.splitlines()[0])
    parser.add_argument("graph", choices=sorted(GRAPHS), help="which graph to run")
    parser.add_argument("--team", required=True, help="team cartridge to resolve")
    parser.add_argument(
        "--cartridges-dir",
        default=REPO_ROOT.parent / "agent-cartridges" / "cartridges",
        help="where cartridge directories live",
    )
    parser.add_argument(
        "--provider-profile",
        default=REPO_ROOT.parent / "agent-cartridges" / "providers" / "anthropic-default.yaml",
    )
    parser.add_argument("--skills-root", action="append", default=[], metavar="PATH")
    parser.add_argument("--unverified-skills", action="store_true", help="skip skill checks; warns every time")
    parser.add_argument("--ticket", help="lifecycle: the ticket to work")
    parser.add_argument("--alerts", help="triage: path to a JSON list of alerts")
    parser.add_argument("--max-alerts", type=int)
    parser.add_argument("--scripted", metavar="JSON", help="run offline against canned node responses")
    parser.add_argument("--assume", choices=["a", "e", "r"], help="answer the gate non-interactively")
    parser.add_argument("--runs-dir", default=REPO_ROOT / "runs")
    parser.add_argument("--ledger", default=REPO_ROOT / "ledger.jsonl")
    parser.add_argument("--worktree-root", help="override the cartridge's worktree_root")
    parser.add_argument("--date", default=date_type.today().isoformat())
    parser.add_argument("--run-id", default=None)
    args = parser.parse_args(argv)

    if not args.skills_root and not args.unverified_skills:
        parser.error("pass --skills-root at least once, or --unverified-skills to skip the check explicitly")

    try:
        cartridge = _resolve_cartridge(args)
    except CartridgeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    run_id = args.run_id or f"{args.graph}-{args.date}-{uuid.uuid4().hex[:8]}"
    graph_args: dict[str, Any] = {"run_id": run_id, "date": args.date, "cartridge": cartridge}

    if args.graph == "lifecycle":
        if not args.ticket:
            parser.error("lifecycle needs --ticket")
        graph_args["ticket"] = args.ticket
    else:
        if not args.alerts:
            parser.error("triage needs --alerts (a JSON list); this graph does not read the queue itself")
        graph_args["alerts"] = json.loads(Path(args.alerts).read_text(encoding="utf-8"))
        if args.max_alerts:
            graph_args["max_alerts"] = args.max_alerts

    module = GRAPHS[args.graph]
    try:
        result = module.run(graph_args, _build_runner(args, cartridge))
    except (ContractViolation, RunnerError) as exc:
        # A contract violation or a dead runner is a bad invocation, and it is
        # reported as one. Anything else is a bug in this code and is allowed to
        # raise with its traceback intact rather than be flattened into "failed".
        print(f"{module.GRAPH_NAME} failed: {exc}", file=sys.stderr)
        return 1

    diffs, human_minutes = _gate(result.get("proposals", []), assume=args.assume)

    # A build patch is applied only after the gate approved the work it belongs to.
    if args.graph == "lifecycle" and result.get("build", {}).get("patch"):
        approved = any(d["decision"] == "approved" for d in diffs)
        if approved:
            root = Path(args.worktree_root or (cartridge.get("landing_areas") or {}).get("worktree_root", "~/worktrees"))
            worktree = Path(str(root)).expanduser() / run_id
            ok, detail = _apply_patch(result["build"]["patch"], worktree)
            print(f"\npatch {'applied in' if ok else 'FAILED to apply in'} {worktree}")
            if not ok:
                print(f"  {detail}", file=sys.stderr)

    manifest = build_manifest(
        run_id=run_id,
        ts=datetime.now(timezone.utc).isoformat(),
        principal=module.GRAPH_NAME,
        cartridge=cartridge,
        provider_profile=Path(args.provider_profile).stem,
        proposals=result.get("proposals", []),
        gate_diffs=diffs,
        human_minutes=human_minutes,
        totals=result.get("totals", {}),
    )
    record_run(manifest, runs_dir=args.runs_dir, ledger_path=args.ledger)

    print(f"\nrecorded {run_id}: {len(diffs)} decision(s), {len(result.get('proposals', []))} proposal(s)")
    print(f"  manifest: {Path(args.runs_dir) / (run_id + '.json')}")
    print(f"  ledger  : {args.ledger}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
