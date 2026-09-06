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
import os
import sys
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from datetime import date as date_type
from pathlib import Path
from typing import Any

import yaml
from core import ledger, workstore
from core.cartridge import CartridgeError
from core.manifest import build_manifest, record_run

from graphs._contract import ContractViolation
from harness.autonomy import split_by_policy
from harness.checks import all_passed, checks_evidence, run_checks
from harness.digest import build_digest
from harness.escalate import escalate_self_modification
from harness.gate import apply_decisions, auto_apply, gate
from harness.phase import run_phase
from harness.registry import GraphSpec, discover
from harness.resolve import overlay_path, resolve_cartridge, role_skill_bodies
from harness.runners import build_runner
from harness.usage import record_usage
from harness.worktree import apply_patch, create_worktree
from runner.protocol import RunnerError

__all__ = ["main"]

REPO_ROOT = Path(__file__).resolve().parent.parent


def _default_ledger() -> Path:
    """Where the trust record lives when nobody says otherwise: OUT of the tree.

    The obvious default is `REPO_ROOT / "ledger.jsonl"`, and it is wrong. This
    system patches its own working tree and, under the epic driver, branches it.
    Path-protection via escalation only governs changes that arrive as
    proposals — a file inside the tree can also be edited by any approved patch
    that claims some other purpose entirely, and a trust record you can reach
    through the very thing it governs is not a record. Out of the tree, no patch
    the system applies can touch it; and `governance_hits` still matches the
    ledger on BASENAME, so a patch that tries to plant a shadow copy inside the
    tree escalates instead of quietly becoming the ledger.

    Read at call time, not import time: `XDG_STATE_HOME` is environment, and
    environment is something a test — or a user — gets to change.
    """
    state = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(state).expanduser() / "agent-graphs" / "ledger.jsonl"


def _observe_trap_failures(
    result: dict[str, Any],
    *,
    graph_name: str,
    ts: str,
    cartridge: dict[str, Any],
    provider_profile: str,
    ledger_path: Path | str,
) -> int:
    """File a `failure` observation for every runbook entry whose trap did not hold.

    Verify is a detector, and detectors file observations. Today its verdict
    reaches a `doc_update` proposal and nothing else, so an entry demonstrated
    wrong IN USE keeps its streak until a human happens to refuse something —
    which is backwards: the run already established the fact, and standing
    should not wait on someone noticing. Rule 3 has always contemplated this
    shape ("a post-hoc detector fired"); this is the detector.

    Deliberately narrow:

    -   `is False` exactly. A missing or None `trap_held` is a graph that did
        not answer, not evidence the trap was wrong, and demoting on silence
        would make the detector punish incomplete runs.
    -   Unverified items observe nothing. An item deferred for capacity was
        never checked; it has no verdict to file.
    -   A subject_new gap — no runbook entry matched — observes nothing. There
        is no streak to demote, and inventing a subject for an entry that does
        not exist yet would create a track record out of its absence.

    Returns how many observations were filed. The row is a `failure` by
    construction (`append_observation` sets the outcome), which per the policy
    resets THAT ENTRY's streak and doubles its bar while every other entry's
    streak stands untouched.
    """
    triaged = result.get("triaged") or []
    hits = [
        entry
        for item in triaged
        if item.get("verified")
        and (item.get("verification") or {}).get("trap_held") is False
        and (entry := str((item.get("classification") or {}).get("runbook_entry") or "").strip())
    ]
    if not hits:
        return 0

    # Risk comes off the taxonomy, never from here. A cartridge that cannot name
    # the kind cannot say what it risks, and a row with an invented risk is a
    # row the policy would count against the wrong bar.
    spec = (cartridge.get("write_kinds") or {}).get("doc_update")
    risk = spec.get("risk") if isinstance(spec, Mapping) else None
    if not risk:
        print(
            f"trap did not hold for {len(hits)} entry(ies) but cartridge "
            f"'{cartridge.get('team', '?')}' declares no risk for 'doc_update'; "
            "no observation recorded — risk is read off the taxonomy, never invented",
            file=sys.stderr,
        )
        return 0

    for entry in hits:
        ledger.append_observation(
            {
                "run_id": result.get("run_id"),
                "ts": ts,
                "principal": graph_name,
                "kind": "doc_update",
                "risk": risk,
                "subject": entry,
                "cartridge_sha": cartridge.get("cartridge_sha"),
                "provider_profile": provider_profile,
            },
            ledger_path,
        )
        print(f"observation: trap did not hold for '{entry}' — recorded against its streak")
    return len(hits)


def _governance_line(hits: list[str], *, label: str = "") -> str:
    where = f"{label}: " if label else ""
    return (
        f"{where}governance paths touched ({len(hits)}): "
        f"{', '.join(hits)} — proposals escalated to self_modification"
    )


def _build_parser(specs: dict[str, GraphSpec]) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="shell.py",
        description="Resolve a cartridge, run a graph, gate its proposals, record the run.",
    )
    parser.add_argument(
        "graph",
        choices=sorted([*specs, "phase", "epic"]),
        help=(
            "which graph to run ('phase' drives the lifecycle graph over one phase; "
            "'epic' drives a whole initiative, phase by phase, gating each one)"
        ),
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
    # Runs stay in the tree — they are artifacts, and an artifact is allowed to
    # be branched away with the work it describes. The ledger is not an
    # artifact; see `_default_ledger`.
    parser.add_argument("--ledger", default=_default_ledger())
    parser.add_argument("--worktree-root", help="override the cartridge's worktree_root")
    parser.add_argument(
        "--resume-from",
        metavar="RUN_ID",
        help=(
            "epic: reuse tasks an earlier run already produced an approved patch for "
            "(saved under runs/<RUN_ID>/tasks/); everything else runs again"
        ),
    )
    parser.add_argument(
        "--repo",
        help=(
            "the repository this change targets; enables the check arm: the patch is "
            "applied in a real worktree of it and the configured checks run there "
            "before the gate"
        ),
    )
    parser.add_argument(
        "--workdir",
        default=REPO_ROOT,
        help=(
            "where a runner whose nodes can read the world stands: the work store root "
            "the apply arms write under (default: this repository)"
        ),
    )
    parser.add_argument("--date", default=date_type.today().isoformat())  # noqa: DTZ011 — the operator's local date is the intended default
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
        elif need.kind == "jsonl_file":
            # One JSON object per line — the ledger's own dialect, parsed with
            # the ledger's own reader so a bad line is named the same way
            # everywhere. The graph gets rows; the file stays on this side.
            out[need.name] = list(ledger.read(raw))
        elif need.kind == "text_or_path":
            path = Path(raw)
            out[need.name] = path.read_text(encoding="utf-8") if path.is_file() else raw
        elif need.kind == "int":
            out[need.name] = int(raw)
        else:
            out[need.name] = raw
    return out


def _cos_docket_args(*, cartridge: Mapping[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    """The kwargs the cos arm hands `assemble_docket` — pulled out so the wire
    from `--runs-dir` (and the cartridge) into the docket it builds is one
    function a test can call directly, rather than something only a full CLI
    invocation could exercise.
    """
    intake_root = next(
        (
            entry.get("path")
            for entry in cartridge.get("intake") or []
            if isinstance(entry, Mapping) and entry.get("source") == "queue_dir" and entry.get("path")
        ),
        None,
    )
    return {
        "intake_root": intake_root,
        "ledger_path": args.ledger,
        "alerts_present": bool(getattr(args, "alerts", None)),
        "cartridge": cartridge,
        "runs_dir": args.runs_dir,
    }


def _read_overlay(repo: str | None) -> Any | None:
    """Parsed `<repo>/.agent/cartridge.yaml`, or None when repo or the file is absent."""
    if not repo or not Path(overlay_path(repo)).is_file():
        return None
    return yaml.safe_load(Path(overlay_path(repo)).read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    specs = discover()
    parser = _build_parser(specs)
    args = parser.parse_args(argv)

    if not args.skills_root and not args.unverified_skills:
        parser.error("pass --skills-root at least once, or --unverified-skills to skip the check explicitly")

    overlay = _read_overlay(args.repo)

    try:
        cartridge, skill_index = resolve_cartridge(
            args.team,
            cartridges_dir=args.cartridges_dir,
            skills_root=args.skills_root,
            unverified_skills=args.unverified_skills,
            overlay=overlay,
        )
    except CartridgeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    runner = build_runner(
        scripted=args.scripted,
        provider_profile=args.provider_profile,
        role_skills=role_skill_bodies(cartridge, skill_index),
        workdir=args.workdir,
        repo=args.repo,
    )

    run_id = args.run_id or f"{args.graph}-{args.date}-{uuid.uuid4().hex[:8]}"

    # A runner whose nodes can read the world gets a tool-computed map of it
    # first, so no node pays turns to draw one. The epic driver refreshes it per
    # phase; this is the single-graph case.
    if args.repo and hasattr(runner, "repo_digest"):
        runner.repo_digest = build_digest(Path(args.repo)) or None
    if hasattr(runner, "check_commands"):
        checks = (cartridge.get("landing_areas") or {}).get("checks") or []
        runner.check_commands = [str(c.get("cmd")) for c in checks if isinstance(c, dict) and c.get("cmd")]

    if args.graph == "epic":
        # The whole initiative. The driver gates and records PER PHASE — phase
        # N+1's base depends on which merges the gate let into phase N's branch,
        # so one gate at the end would be deciding after the ground had already
        # been chosen. Everything below this block (policy split, gate, record)
        # is therefore already done by the time run_epic returns, and this
        # branch returns instead of falling through to do it twice.
        from harness.epic import run_epic

        if not args.initiative:
            parser.error("epic needs --initiative (a work/<initiative> directory)")
        if not args.repo:
            parser.error("epic needs --repo (the repository the initiative's work targets)")
        try:
            initiative = workstore.read_initiative(args.initiative)
        except workstore.WorkStoreError as exc:
            print(str(exc), file=sys.stderr)
            return 1

        result = run_epic(
            initiative=initiative,
            repo=Path(args.repo),
            cartridge=cartridge,
            runner=runner,
            specs=specs,
            run_id=run_id,
            date=args.date,
            max_parallel=args.max_parallel,
            ledger_path=args.ledger,
            provider_profile=Path(args.provider_profile).stem,
            runs_dir=args.runs_dir,
            worktree_root=args.worktree_root
            or (cartridge.get("landing_areas") or {}).get("worktree_root", "~/worktrees"),
            assume=args.assume,
            fix_attempts=args.fix_attempts,
            resume_from=args.resume_from,
        )
        totals = result.get("totals") or {}
        print(
            f"\nepic {run_id}: {totals.get('phases_complete', 0)} phase(s) complete, "
            f"{totals.get('phases_partial', 0)} partial, {totals.get('phases_blocked', 0)} blocked, "
            f"{totals.get('tasks_quarantined', 0)} task(s) quarantined, "
            f"{totals.get('stacks_rebased', 0)} stack(s) rebased"
        )
        for entry in result.get("quarantined") or []:
            print(f"  quarantined {entry.get('grain')}: {entry.get('id')} — {entry.get('reason')}", file=sys.stderr)
        print(f"  manifests: {args.runs_dir} (one per phase, under {run_id}:<phase>)")
        print(f"  ledger   : {args.ledger}")
        record_usage(runner, runs_dir=args.runs_dir, run_id=run_id)
        return 0

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
    elif args.graph == "cos" and not getattr(args, "docket", None):
        # The coxswain driver path. With --docket the cos graph runs alone
        # through the generic arm below — judgment only, nothing invoked. Without
        # it, the driver assembles the docket from what is actually readable
        # (intake queue, ledger, registry), runs the dispatch graph, and invokes
        # what it selected through the nested-invocation primitive, so every
        # dispatched proposal lands in the same policy/gate/record as any other.
        from harness.cos import CosError, assemble_docket, run_cos

        # `--runs-dir` is the same global flag every other arm already writes
        # manifests under; reusing it here (default: the real runs directory)
        # is safe because assemble_docket only counts a live `*.pid` as
        # in flight — everything else already down there is ignored.
        docket_args = _cos_docket_args(cartridge=cartridge, args=args)
        docket = assemble_docket(specs=specs, **docket_args)
        alerts = (
            json.loads(Path(args.alerts).read_text(encoding="utf-8")) if getattr(args, "alerts", None) else None
        )
        try:
            cos_out = run_cos(
                docket=docket,
                specs=specs,
                runner=runner,
                cartridge=cartridge,
                run_id=run_id,
                date=args.date,
                max_parallel=args.max_parallel,
                intake_root=docket_args["intake_root"],
                ledger_path=args.ledger,
                alerts=alerts,
            )
        except (ContractViolation, RunnerError, CosError) as exc:
            print(f"coxswain failed: {exc}", file=sys.stderr)
            return 1
        for failure in cos_out["failures"]:
            print(f"dispatched run failed: {failure}", file=sys.stderr)
        deferred = cos_out["deferred"]
        invoked = cos_out["invoked"]
        if invoked:
            picked = ", ".join(str(s.get("graph")) for s in invoked)
        elif deferred:
            picked = "nothing (at capacity)"
        else:
            picked = "nothing (idle)"
        print(f"coxswain dispatched: {picked}")
        if deferred:
            reasons = "; ".join(f"{d['graph']} ({d['reason']})" for d in deferred)
            print(f"coxswain deferred: {reasons}")
        if cos_out["consumed"]:
            print(f"intake consumed: {', '.join(cos_out['consumed'])}")
        graph_name = "coxswain(dispatch)"
        result = {
            "run_id": run_id,
            "date": args.date,
            "selections": cos_out["selections"],
            "results": cos_out["results"],
            "proposals": cos_out["proposals"],
            "consumed": cos_out["consumed"],
            "deferred": deferred,
            "totals": {
                "selected": len(cos_out["selections"]),
                "completed": len(cos_out["results"]),
                "failed": len(cos_out["failures"]),
                "consumed": len(cos_out["consumed"]),
                "deferred": len(deferred),
            },
        }
    else:
        spec = specs[args.graph]
        graph_name = spec.graph_name
        if args.graph == "retro" and getattr(args, "ledger_rows", None) is None:
            # The rows retro reasons over default to the ledger this harness
            # already keeps. Explicit --ledger-rows still points it anywhere —
            # a retro over some other record is a legitimate ask — but the graph
            # itself never reads either; the harness does, right here.
            args.ledger_rows = str(args.ledger)
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

    # The check arm. Only when --repo names the project this change targets,
    # and only for the single-graph lifecycle path — the epic driver arriving
    # separately owns fanning this out across a phase's per-task results. It
    # runs BEFORE the policy and the gate see anything: evidence attached
    # after the decision is already made is decoration, not evidence.
    if args.repo and args.graph == "lifecycle" and result.get("build", {}).get("patch"):
        root = Path(args.worktree_root or (cartridge.get("landing_areas") or {}).get("worktree_root", "~/worktrees"))
        worktree = Path(str(root)).expanduser() / run_id
        targets = [p for p in proposals if p.get("kind") == "draft_pr_create"]

        wt_ok, wt_detail = create_worktree(Path(args.repo), worktree, branch=f"agents/{run_id}")
        if not wt_ok:
            print(f"\nworktree FAILED for agents/{run_id}: {wt_detail}", file=sys.stderr)
            for item in targets:
                item.setdefault("evidence", []).append({"check": "patch_apply", "output": f"FAIL — {wt_detail}"})
        else:
            ok, detail = apply_patch(result["build"]["patch"], worktree)
            print(f"\npatch {'applied in' if ok else 'FAILED to apply in'} {worktree}")
            if not ok:
                print(f"  {detail}", file=sys.stderr)
                for item in targets:
                    item.setdefault("evidence", []).append({"check": "patch_apply", "output": f"FAIL — {detail}"})
            else:
                evidence_rows = [{"check": "patch_apply", "output": f"ok — applied in {worktree}"}]
                checks_config = (cartridge.get("landing_areas") or {}).get("checks") or []
                if not checks_config:
                    print("no checks configured; the gate decides on review evidence alone")
                else:
                    check_results = run_checks(worktree, checks_config)
                    result["checks"] = check_results
                    for r in check_results:
                        print(f"  check {r['name']}: {'pass' if r['passed'] else 'FAIL'} (exit {r['exit_code']})")
                    print(f"  checks overall: {'all passed' if all_passed(check_results) else 'FAILURES present'}")
                    evidence_rows.extend(checks_evidence(check_results))
                for item in targets:
                    item.setdefault("evidence", []).extend(evidence_rows)

    # Escalation, from the patch's paths alone. Here because it must land AFTER
    # the graph named its kinds and the check arm attached its verdict — the
    # gate should see the tests' opinion of a governance change too — and
    # BEFORE the policy split, which is the only window where no streak on a
    # mundane kind can carry an edit to the rules past the gate.
    if args.graph == "phase":
        escalated: list[dict[str, Any]] = []
        for r in results:
            slice_, hits = escalate_self_modification(
                r.get("proposals", []),
                patch=r.get("build", {}).get("patch") or "",
                cartridge=cartridge,
                ledger_path=args.ledger,
            )
            r["proposals"] = slice_
            if hits:
                print(_governance_line(hits, label=str(r.get("ticket") or "task")))
            escalated.extend(slice_)
        proposals = result["proposals"] = escalated
    elif result.get("build", {}).get("patch"):
        proposals, hits = escalate_self_modification(
            proposals, patch=result["build"]["patch"], cartridge=cartridge, ledger_path=args.ledger
        )
        result["proposals"] = proposals
        if hits:
            print(_governance_line(hits))

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

    # A build patch is applied only after the gate approved the work it belongs
    # to. Skipped when --repo already ran the check arm above: the work is
    # already applied in a real worktree, and applying it again into the old
    # scratch dir would be redundant at best and misleading at worst.
    if not args.repo and args.graph == "lifecycle" and result.get("build", {}).get("patch"):
        approved = any(d["decision"] == "approved" for d in diffs)
        if approved:
            root = Path(args.worktree_root or (cartridge.get("landing_areas") or {}).get("worktree_root", "~/worktrees"))
            worktree = Path(str(root)).expanduser() / run_id
            ok, detail = apply_patch(result["build"]["patch"], worktree)
            print(f"\npatch {'applied in' if ok else 'FAILED to apply in'} {worktree}")
            if not ok:
                print(f"  {detail}", file=sys.stderr)

    ts = datetime.now(UTC).isoformat()
    manifest = build_manifest(
        run_id=run_id,
        ts=ts,
        principal=graph_name,
        cartridge=cartridge,
        provider_profile=provider_profile,
        proposals=proposals,
        gate_diffs=diffs,
        human_minutes=human_minutes,
        totals={**result.get("totals", {}), "auto_applied": len(auto_applied), "gated": len(gated)},
    )
    record_run(manifest, runs_dir=args.runs_dir, ledger_path=args.ledger)

    # And then what the run itself established, on the same clock as the
    # manifest. This lands AFTER record_run because it is a post-hoc verdict on
    # a run already recorded, not a second opinion on the gate.
    _observe_trap_failures(
        result,
        graph_name=graph_name,
        ts=ts,
        cartridge=cartridge,
        provider_profile=provider_profile,
        ledger_path=args.ledger,
    )

    print(f"\nrecorded {run_id}: {len(auto_applied)} auto-applied, {len(diffs)} gated decision(s), {len(proposals)} proposal(s)")
    print(f"  manifest: {Path(args.runs_dir) / (run_id + '.json')}")
    print(f"  ledger  : {args.ledger}")
    record_usage(runner, runs_dir=args.runs_dir, run_id=run_id)
    close = getattr(runner, "close", None)
    if callable(close):
        close()
    return 0
