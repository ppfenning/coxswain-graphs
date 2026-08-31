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
from collections import Counter
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date as date_type
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core import ledger
from core.cartridge import CartridgeError, load
from core.manifest import build_manifest, gate_diff, record_run
from core.policy import AUTO, autonomy_policy
from core.skills import index_from_roots

from core import workstore
from graphs import epic_reconcile, initiative_decompose, lifecycle_propose, triage_propose
from graphs._contract import ContractViolation
from runner import ScriptedRunner
from runner.protocol import RunnerError

GRAPHS = {
    "lifecycle": lifecycle_propose,
    "triage": triage_propose,
    "reconcile": epic_reconcile,
    "decompose": initiative_decompose,
    # `phase` is not a graph. It runs the lifecycle graph once per ready task,
    # concurrently, and is handled separately below.
    "phase": lifecycle_propose,
}

REPO_ROOT = Path(__file__).resolve().parent

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


def _run_phase(
    *,
    tasks: list[dict[str, Any]],
    cartridge: dict[str, Any],
    runner: Any,
    run_id: str,
    date: str,
    max_parallel: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Run the lifecycle graph once per ready task, at the same time.

    Concurrency lives here and nowhere else. The graphs stay pure and
    single-task; this is the I/O edge, and running several HTTP-bound node
    sequences at once is exactly what an I/O edge is for.

    Results are collected into a dict and read back IN TASK-ID ORDER, so two
    runs over the same work produce the same manifest no matter who finished
    first. If wall-clock order could reach the ledger, the record would stop
    being a record and become a race.
    """
    results: dict[str, dict[str, Any]] = {}
    failures: list[str] = []

    with ThreadPoolExecutor(max_workers=max(1, max_parallel)) as pool:
        futures = {
            pool.submit(
                lifecycle_propose.run,
                {
                    "run_id": f"{run_id}:{task['id']}",
                    "date": date,
                    "ticket": task["id"],
                    "cartridge": cartridge,
                    "surfaces": task.get("surfaces") or [],
                },
                runner,
            ): task
            for task in tasks
        }
        for future in as_completed(futures):
            task = futures[future]
            try:
                results[task["id"]] = future.result()
            except (ContractViolation, RunnerError) as exc:
                # One task failing must not take the phase with it. The others
                # already ran; their work is still worth gating.
                failures.append(f"{task['id']}: {exc}")

    ordered = [results[key] for key in sorted(results)]
    proposals = [p for result in ordered for p in result.get("proposals", [])]
    return ordered, proposals, sorted(failures)


def _split_by_policy(
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
        config = {**base, "applied_this_run": applied_so_far[item["kind"]]}
        if autonomy_policy(item["kind"], item["risk"], rows, config) == AUTO:
            auto.append(item)
            applied_so_far[item["kind"]] += 1
        else:
            gated.append(item)
    return auto, gated


def _apply_arm_for(kind: str, cartridge: dict[str, Any]) -> str | None:
    spec = (cartridge.get("write_kinds") or {}).get(kind)
    return spec.get("apply_arm") if isinstance(spec, Mapping) else None


def _auto_apply(
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
    arm = _apply_arm_for(item["kind"], cartridge)
    if arm in (None, "pr"):
        return False, f"no executable apply arm for '{item['kind']}' (arm: {arm})"
    if arm == "shell":
        return False, "shell-armed kinds are applied by the run path that owns them"
    result = runner.run(
        role=arm,
        tier="standard",
        schema=APPLY_SCHEMA,
        context=list(cartridge.get("context") or []),
        prompt=(
            f"Apply this approved proposal exactly as written. Do not widen it.\n\n"
            f"kind: {item['kind']}\ntarget: {item['target']}\n"
            f"action: {item['suggested_action']}\nevidence: {item['evidence']}"
        ),
    )
    return bool(result.get("applied")), str(result.get("detail", ""))


def _gate(proposals: list[dict[str, Any]], *, assume: str | None) -> tuple[list[dict[str, Any]], float]:
    """Present each proposal and capture a decision. Nothing applies without one.

    `human_minutes` is entered here, at the gate, not reconstructed later. A time
    saving recalled a month afterwards convinces nobody.
    """
    if not proposals:
        return [], 0.0

    decisions: list[tuple[dict[str, Any], str, bool]] = []
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
        decisions.append((item, decision, edited))

    minutes = (datetime.now(timezone.utc) - started).total_seconds() / 60
    return decisions, round(minutes, 2)


def _apply_decisions(
    decisions: list[tuple[dict[str, Any], str, bool]],
    *,
    cartridge: dict[str, Any],
    runner: Any,
) -> list[dict[str, Any]]:
    """Execute what the gate approved, then record what ACTUALLY happened.

    Approval is not execution. The earlier version passed `applied=decision ==
    "approved"` straight into `gate_diff`, so the ledger recorded `clean` for
    proposals nothing had ever run — a self-report, which is the one thing the
    ledger exists not to accept. `applied` now comes from the arm returning
    successfully, and an approved proposal that could not be executed records
    `skipped`, which is exactly what it is.
    """
    diffs: list[dict[str, Any]] = []
    for item, decision, edited in decisions:
        applied = False
        if decision == "approved":
            applied, detail = _auto_apply(item, cartridge=cartridge, runner=runner)
            if not applied:
                print(f"  approved but not executed ({detail})", file=sys.stderr)
        diffs.append(gate_diff(item, decision, applied=applied, edited=edited))
    return diffs


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
    parser.add_argument("--epic", help="reconcile: path to the epic's DECLARED state, as JSON")
    parser.add_argument("--observed", help="reconcile: path to the board's ACTUAL state, as JSON")
    parser.add_argument("--idea", help="decompose: the initiative, as prose or a path to it")
    parser.add_argument("--initiative", help="phase: path to the work/<initiative> directory")
    parser.add_argument("--phase-name", help="phase: which phase to run (default: the first with ready work)")
    parser.add_argument("--max-parallel", type=int, default=4, help="phase: how many tasks run at once")
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
    elif args.graph == "decompose":
        if not args.idea:
            parser.error("decompose needs --idea (prose, or a path to a file holding it)")
        idea = Path(args.idea)
        graph_args["idea"] = idea.read_text(encoding="utf-8") if idea.is_file() else args.idea
    elif args.graph == "phase":
        if not args.initiative:
            parser.error("phase needs --initiative (a work/<initiative> directory)")
    elif args.graph == "reconcile":
        if not (args.epic and args.observed):
            parser.error("reconcile needs --epic and --observed; this graph does not read the tracker itself")
        graph_args["epic"] = json.loads(Path(args.epic).read_text(encoding="utf-8"))
        graph_args["observed"] = json.loads(Path(args.observed).read_text(encoding="utf-8"))
    else:
        if not args.alerts:
            parser.error("triage needs --alerts (a JSON list); this graph does not read the queue itself")
        graph_args["alerts"] = json.loads(Path(args.alerts).read_text(encoding="utf-8"))
        if args.max_alerts:
            graph_args["max_alerts"] = args.max_alerts

    module = GRAPHS[args.graph]
    runner = _build_runner(args, cartridge)

    if args.graph == "phase":
        # Not one graph run but many, one per unblocked task. The work store is
        # read HERE and the tasks handed in as arguments, because a graph that
        # reads the filesystem cannot be replayed.
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
        results, proposals, failures = _run_phase(
            tasks=ready,
            cartridge=cartridge,
            runner=runner,
            run_id=run_id,
            date=args.date,
            max_parallel=args.max_parallel,
        )
        for failure in failures:
            print(f"task failed: {failure}", file=sys.stderr)
        result = {
            "run_id": run_id,
            "phase": phase_name,
            "tasks": [r.get("ticket") for r in results],
            "proposals": proposals,
            "totals": {"ready": len(ready), "completed": len(results), "failed": len(failures)},
        }
    else:
        try:
            result = module.run(graph_args, runner)
        except (ContractViolation, RunnerError) as exc:
            # A contract violation or a dead runner is a bad invocation, and it
            # is reported as one. Anything else is a bug in this code and is
            # allowed to raise with its traceback intact rather than be
            # flattened into "failed".
            print(f"{module.GRAPH_NAME} failed: {exc}", file=sys.stderr)
            return 1

    provider_profile = Path(args.provider_profile).stem
    proposals = result.get("proposals", [])

    # Consult the policy BEFORE the human sees anything. Without this the gate
    # asks about every kind forever, no streak is ever spent, and the whole
    # earned-autonomy argument is decoration.
    auto, gated = _split_by_policy(
        proposals, cartridge=cartridge, ledger_path=args.ledger, provider_profile=provider_profile
    )

    auto_applied: list[dict[str, Any]] = []
    for item in auto:
        ok, detail = _auto_apply(item, cartridge=cartridge, runner=runner)
        if ok:
            print(f"auto-applied {item['kind']} -> {item['target']}: {detail}")
            auto_applied.append(item)
        else:
            # Cleared by policy but nothing here can execute it. It goes to the
            # gate rather than being reported as done.
            print(f"auto-eligible but not executed ({detail}); sending to the gate", file=sys.stderr)
            gated.append(item)

    decisions, human_minutes = _gate(gated, assume=args.assume)
    diffs = _apply_decisions(decisions, cartridge=cartridge, runner=runner)

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


if __name__ == "__main__":
    raise SystemExit(main())
