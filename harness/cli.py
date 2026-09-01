"""The command line over the harness.

Thin on purpose. Everything here is argument plumbing; the machinery it drives
lives in the sibling modules, and the graphs it offers come from discovery
rather than a dispatch table — `python shell.py <graph>` works for any module
under `graphs/` that declares a SPEC.

`phase` is the one subcommand that is not a graph: it is the harness's own
driver, running the lifecycle graph once per ready task, concurrently.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import date as date_type
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core import workstore
from core.cartridge import CartridgeError
from core.manifest import build_manifest, record_run
from graphs._contract import ContractViolation
from harness.autonomy import split_by_policy
from harness.gate import apply_decisions, auto_apply, gate
from harness.phase import run_phase
from harness.registry import GraphSpec, discover
from harness.resolve import resolve_cartridge, role_skill_bodies
from harness.runners import build_runner
from harness.worktree import apply_patch
from runner.protocol import RunnerError

__all__ = ["main"]

REPO_ROOT = Path(__file__).resolve().parent.parent


def _build_parser(specs: dict[str, GraphSpec]) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="shell.py",
        description="Resolve a cartridge, run a graph, gate its proposals, record the run.",
    )
    parser.add_argument(
        "graph",
        choices=sorted([*specs, "phase"]),
        help="which graph to run ('phase' drives the lifecycle graph over a work store)",
    )
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

    # Every graph's declared needs become flags. Two graphs may not claim the
    # same flag with different meanings; identical re-declarations collapse.
    seen: dict[str, str] = {}
    for spec in specs.values():
        for need in spec.needs:
            if need.flag in seen:
                if seen[need.flag] != f"{need.kind}:{need.name}":
                    raise SystemExit(
                        f"graph '{spec.name}' redefines {need.flag} with a different meaning"
                    )
                continue
            seen[need.flag] = f"{need.kind}:{need.name}"
            kwargs: dict[str, Any] = {"help": f"{spec.name}: {need.help}" if need.help else None}
            if need.kind == "int":
                kwargs["type"] = int
            parser.add_argument(need.flag, **{k: v for k, v in kwargs.items() if v is not None})

    parser.add_argument("--initiative", help="phase: path to the work/<initiative> directory")
    parser.add_argument("--phase-name", help="phase: which phase to run (default: the first with ready work)")
    parser.add_argument("--max-parallel", type=int, default=4, help="phase: how many tasks run at once")
    parser.add_argument("--scripted", metavar="JSON", help="run offline against canned node responses")
    parser.add_argument("--assume", choices=["a", "e", "r"], help="answer the gate non-interactively")
    parser.add_argument("--runs-dir", default=REPO_ROOT / "runs")
    parser.add_argument("--ledger", default=REPO_ROOT / "ledger.jsonl")
    parser.add_argument("--worktree-root", help="override the cartridge's worktree_root")
    parser.add_argument("--date", default=date_type.today().isoformat())
    parser.add_argument("--run-id", default=None)
    return parser


def _materialise(spec: GraphSpec, args: argparse.Namespace, parser: argparse.ArgumentParser) -> dict[str, Any]:
    """Turn a spec's declared needs into graph args. All I/O happens HERE.

    The spec says `json_file`; the harness reads and parses the file. The graph
    module never touches the filesystem, which is what lets the portability
    suite hold it to that.
    """
    out: dict[str, Any] = {}
    for need in spec.needs:
        raw = getattr(args, need.flag.lstrip("-").replace("-", "_"), None)
        if raw is None:
            if need.required:
                parser.error(f"{spec.name} needs {need.flag}" + (f" ({need.help})" if need.help else ""))
            continue
        if need.kind == "json_file":
            out[need.name] = json.loads(Path(raw).read_text(encoding="utf-8"))
        elif need.kind == "text_or_path":
            path = Path(raw)
            out[need.name] = path.read_text(encoding="utf-8") if path.is_file() else raw
        elif need.kind == "int":
            out[need.name] = int(raw)
        else:
            out[need.name] = raw
    return out


def main(argv: list[str] | None = None) -> int:
    specs = discover()
    parser = _build_parser(specs)
    args = parser.parse_args(argv)

    if not args.skills_root and not args.unverified_skills:
        parser.error("pass --skills-root at least once, or --unverified-skills to skip the check explicitly")

    try:
        cartridge, skill_index = resolve_cartridge(
            args.team,
            cartridges_dir=args.cartridges_dir,
            skills_root=args.skills_root,
            unverified_skills=args.unverified_skills,
        )
    except CartridgeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    runner = build_runner(
        scripted=args.scripted,
        provider_profile=args.provider_profile,
        role_skills=role_skill_bodies(cartridge, skill_index),
    )

    run_id = args.run_id or f"{args.graph}-{args.date}-{uuid.uuid4().hex[:8]}"

    if args.graph == "phase":
        # Not one graph run but many, one per unblocked task. The work store is
        # read HERE and the tasks handed in as arguments, because a graph that
        # reads the filesystem cannot be replayed.
        if not args.initiative:
            parser.error("phase needs --initiative (a work/<initiative> directory)")
        try:
            initiative = workstore.read_initiative(args.initiative)
        except workstore.WorkStoreError as exc:
            print(str(exc), file=sys.stderr)
            return 1

        phase_name = args.phase_name
        if phase_name is None:
            phase_name = next(
                (p for p in initiative["phases"] if workstore.ready_tasks(initiative["items"], phase=p)), None
            )
        ready = workstore.ready_tasks(initiative["items"], phase=phase_name) if phase_name else []
        if not ready:
            print(f"nothing ready in {initiative['id']}" + (f" phase {phase_name}" if phase_name else ""))
            return 0

        print(f"phase {phase_name}: {len(ready)} task(s) ready, running up to {args.max_parallel} at once")
        print("  " + ", ".join(t["id"] for t in ready))
        results, proposals, failures = run_phase(
            lifecycle_run=specs["lifecycle"].run,
            tasks=ready,
            cartridge=cartridge,
            runner=runner,
            run_id=run_id,
            date=args.date,
            max_parallel=args.max_parallel,
        )
        for failure in failures:
            print(f"task failed: {failure}", file=sys.stderr)
        graph_name = "phase(lifecycle-propose)"
        result = {
            "run_id": run_id,
            "phase": phase_name,
            "tasks": [r.get("ticket") for r in results],
            "proposals": proposals,
            "totals": {"ready": len(ready), "completed": len(results), "failed": len(failures)},
        }
    else:
        spec = specs[args.graph]
        graph_name = spec.graph_name
        graph_args: dict[str, Any] = {"run_id": run_id, "date": args.date, "cartridge": cartridge}
        graph_args.update(_materialise(spec, args, parser))
        try:
            result = spec.run(graph_args, runner)
        except (ContractViolation, RunnerError) as exc:
            # A contract violation or a dead runner is a bad invocation, and it
            # is reported as one. Anything else is a bug in this code and is
            # allowed to raise with its traceback intact rather than be
            # flattened into "failed".
            print(f"{graph_name} failed: {exc}", file=sys.stderr)
            return 1

    provider_profile = Path(args.provider_profile).stem
    proposals = result.get("proposals", [])

    # Consult the policy BEFORE the human sees anything. Without this the gate
    # asks about every kind forever, no streak is ever spent, and the whole
    # earned-autonomy argument is decoration.
    auto, gated = split_by_policy(
        proposals, cartridge=cartridge, ledger_path=args.ledger, provider_profile=provider_profile
    )

    auto_applied: list[dict[str, Any]] = []
    for item in auto:
        ok, detail = auto_apply(item, cartridge=cartridge, runner=runner)
        if ok:
            print(f"auto-applied {item['kind']} -> {item['target']}: {detail}")
            auto_applied.append(item)
        else:
            # Cleared by policy but nothing here can execute it. It goes to the
            # gate rather than being reported as done.
            print(f"auto-eligible but not executed ({detail}); sending to the gate", file=sys.stderr)
            gated.append(item)

    decisions, human_minutes = gate(gated, assume=args.assume)
    diffs = apply_decisions(decisions, cartridge=cartridge, runner=runner)

    # A build patch is applied only after the gate approved the work it belongs to.
    if args.graph == "lifecycle" and result.get("build", {}).get("patch"):
        approved = any(d["decision"] == "approved" for d in diffs)
        if approved:
            root = Path(args.worktree_root or (cartridge.get("landing_areas") or {}).get("worktree_root", "~/worktrees"))
            worktree = Path(str(root)).expanduser() / run_id
            ok, detail = apply_patch(result["build"]["patch"], worktree)
            print(f"\npatch {'applied in' if ok else 'FAILED to apply in'} {worktree}")
            if not ok:
                print(f"  {detail}", file=sys.stderr)

    manifest = build_manifest(
        run_id=run_id,
        ts=datetime.now(timezone.utc).isoformat(),
        principal=graph_name,
        cartridge=cartridge,
        provider_profile=provider_profile,
        proposals=proposals,
        gate_diffs=diffs,
        human_minutes=human_minutes,
        totals={**result.get("totals", {}), "auto_applied": len(auto_applied), "gated": len(gated)},
    )
    record_run(manifest, runs_dir=args.runs_dir, ledger_path=args.ledger)

    print(f"\nrecorded {run_id}: {len(auto_applied)} auto-applied, {len(diffs)} gated decision(s), {len(proposals)} proposal(s)")
    print(f"  manifest: {Path(args.runs_dir) / (run_id + '.json')}")
    print(f"  ledger  : {args.ledger}")
    return 0
