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
import contextlib
import hashlib
import json
import os
import signal
import socket
import sys
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from datetime import date as date_type
from pathlib import Path
from typing import Any

import core
import yaml
from core import ledger, workstore
from core.cartridge import CartridgeError
from core.manifest import append_ledger, build_manifest

from graphs._contract import ContractViolation
from harness import CORE_SCHEMA, run_lease, store_traces
from harness.autonomy import split_by_policy
from harness.checks import all_passed, checks_evidence, run_checks
from harness.digest import build_digest
from harness.escalate import escalate_self_modification
from harness.gate import apply_decisions, auto_apply, gate
from harness.phase import run_phase
from harness.registry import GraphSpec, discover
from harness.resolve import overlay_path, resolve_cartridge, role_skill_bodies
from harness.runners import build_runner
from harness.store_dialect import default_url
from harness.store_graphs import derive_definition, register
from harness.store_lease import acquire, release
from harness.store_migrate import open_store
from harness.store_read import calls as node_calls
from harness.store_read import cost_by_model, run_summary
from harness.store_write import Store
from harness.traces_url import have_pyarrow, redact_url, resolve_traces_root
from harness.worktree import apply_patch, create_worktree, keep_worktree, remove_worktree
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
                "overlay_sha": cartridge.get("overlay_sha"),
                "provider_profile": provider_profile,
                "schema": core.SCHEMA_VERSION,
            },
            ledger_path,
        )
        print(f"observation: trap did not hold for '{entry}' — recorded against its streak")
    return len(hits)


def _core_schema_status(installed: str, required: str) -> tuple[str, str] | None:
    """Compare MAJOR.MINOR core schema strings; None means proceed silently.

    Otherwise ("fatal", message) on a MAJOR difference or ("warn", message)
    on a MINOR-only difference.
    """
    installed_major, installed_minor = (int(p) for p in installed.split(".", 1))
    required_major, required_minor = (int(p) for p in required.split(".", 1))
    if installed_major != required_major:
        return "fatal", f"core schema {installed} does not match harness CORE_SCHEMA {required}; upgrade coxswain-graphs"
    if installed_minor != required_minor:
        return "warn", f"core schema {installed} does not match harness CORE_SCHEMA {required}"
    return None


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
        choices=sorted([*specs, "phase", "epic", "sweep"]),
        help=(
            "which graph to run ('phase' drives the lifecycle graph over one phase; "
            "'epic' drives a whole initiative, phase by phase, gating each one; "
            "'sweep' is registered for coxswain dispatch but not yet runnable standalone)"
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
        "--node-cap-usd",
        type=float,
        default=None,
        help=(
            "operator's per-node spend cap; the effective limit becomes "
            "min(shape ceiling, this) and a stop at the cap is error_spend_cap"
        ),
    )
    parser.add_argument(
        "--keep-worktrees",
        action="store_true",
        help="on exit, move the run's worktree under <worktree_root>/_kept/<run_id> instead of deleting it",
    )
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
    parser.add_argument("--result-out", default=None, help="write the graph's result as JSON to this path")
    return parser


def _provider_profile_scope(path: Path | str) -> str:
    """`stem@sha12` of the resolved profile file's bytes (triage.md §4)."""
    resolved = Path(path)
    digest = hashlib.sha256(resolved.read_bytes()).hexdigest()[:12]
    return f"{resolved.stem}@{digest}"


_PHASE_PRINCIPAL = "phase(lifecycle-propose)"
_COS_PRINCIPAL = "coxswain(dispatch)"
_LEASE_TTL = 120  # seconds; the heartbeat renews every 30


def _read_profile(path: Path | str) -> Mapping[str, Any]:
    """The provider profile's mapping; empty when unreadable, since the scripted path may name no real file."""
    try:
        data = yaml.safe_load(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, Mapping) else {}


def _storage_url(profile: Mapping[str, Any], runs_dir: Path | str) -> str:
    """The profile's `storage_url`; without one, the sqlite file inside the runs directory."""
    url = profile.get("storage_url")
    return url if isinstance(url, str) and url else default_url(runs_dir)


def _traces_url(profile: Mapping[str, Any], runs_dir: Path | str) -> str | None:
    """The profile's `traces_url`; None when absent, which leaves the root at `<runs_dir>/traces`."""
    url = profile.get("traces_url")
    return url if isinstance(url, str) and url else None


def _principal(graph: str, specs: Mapping[str, GraphSpec], *, docket: str | None) -> str:
    """The run's principal, named as the manifest names it. `epic` is the driver's own constant."""
    if graph == "epic":
        from harness.epic import PRINCIPAL

        return PRINCIPAL
    if graph == "phase":
        return _PHASE_PRINCIPAL
    if graph == "cos" and not docket:
        return _COS_PRINCIPAL
    return specs[graph].graph_name if graph in specs else graph


def _register_graph(conn: Any, specs: Mapping[str, GraphSpec], graph: str, now: str) -> str | None:
    """Register the graph this run executes and return its id.

    unknown: no graph in this repository declares a loaded graph object yet. A spec that
    carries one on `.graph` is registered; one that does not leaves the run's graph_id empty.
    """
    spec = specs.get("lifecycle" if graph in ("phase", "epic") else graph)
    loaded = getattr(spec, "graph", None)
    return None if loaded is None else register(conn, derive_definition(loaded), now)


def _begin_store_run(
    args: argparse.Namespace,
    specs: Mapping[str, GraphSpec],
    cartridge: Mapping[str, Any],
    run_id: str,
    now: str,
    host: str,
) -> Store | None:
    """Open the run-record store, register the graph and write the run row; None (after one line) if any of it fails."""
    url = _storage_url(_read_profile(args.provider_profile), args.runs_dir)
    conn = None
    try:
        if url == default_url(args.runs_dir):
            Path(args.runs_dir).mkdir(parents=True, exist_ok=True)
        conn = open_store(url, now)
        graph_id = _register_graph(conn, specs, args.graph, now)
        try:
            provider_profile = _provider_profile_scope(args.provider_profile)
        except OSError:
            provider_profile = str(args.provider_profile)
        record = {
            "run_id": run_id,
            "principal": _principal(args.graph, specs, docket=getattr(args, "docket", None)),
            "cartridge_sha": cartridge.get("cartridge_sha"),
            "cartridge_team": cartridge.get("team"),
            "overlay_sha": cartridge.get("overlay_sha"),
            "provider_profile": provider_profile,
        }
        launch = {
            "launched_by": os.environ.get("AGENT_GRAPHS_LAUNCHED_BY") or "cli",
            "at": now,
            "graph_id": graph_id,
        }
        store = Store(conn)
        store.record_run(record, launch, host=host)
    except Exception as exc:
        if conn is not None:
            conn.close()
        print(f"store: cannot use {url.split(':', 1)[0]} store: {' '.join(str(exc).split())}", file=sys.stderr)
        return None
    return store


def _refuse_start(store: Store, run_id: str, status: str, code: int) -> int:
    """Stamp a run that never started and close its store; `code` is its exit."""
    _finish_store_run(store, run_id, datetime.now(UTC).isoformat(), status)
    store.conn.close()
    return code


def _end_lease(store: Store, heartbeat: run_lease.Heartbeat, name: str, holder: str, epoch: int) -> None:
    """Stop renewing, then expire the lease. Warns on any fault and never raises; an unreleased lease expires."""
    heartbeat.stop()
    try:
        if not release(store.conn, name, holder, epoch):
            print(f"lease: {name} was not released: it is no longer held by {holder}", file=sys.stderr)
    except Exception as exc:
        print(f"lease: could not release {name}: {' '.join(str(exc).split())}", file=sys.stderr)


def _finish_store_run(store: Store, run_id: str, ended_at: str, status: str) -> None:
    """Stamp the run's end. A store that fails here warns; it never changes the run's exit."""
    try:
        store.finish_run(run_id, ended_at, status)
    except Exception as exc:
        print(f"store: could not record the end of {run_id}: {' '.join(str(exc).split())}", file=sys.stderr)


def _trace_path(detail: Any) -> Path | None:
    """The trace file a call's `detail_json` names, or None when it names none. Accepts the JSON text or a decoded object."""
    if isinstance(detail, str):
        try:
            detail = json.loads(detail)
        except json.JSONDecodeError:
            return None
    trace = detail.get("trace") if isinstance(detail, dict) else None
    return Path(trace) if isinstance(trace, str) and trace else None


def _read_events(text: str) -> list[dict[str, Any]]:
    """The JSON-object lines of a trace file's text; a blank, malformed or non-object line is skipped."""
    events = []
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _compact_traces(store: Store, run_id: str, runs_dir: Path, traces_url: str | None = None) -> None:
    """Write this run's trace files as one Parquet file; delete them only once write_run's row count equals the events read.

    Warns and never raises. Any failure leaves every loose file, so a rerun rewrites the same Parquet file.
    """
    try:
        rows = node_calls(store.conn, run_id)
    except Exception as exc:
        print(f"traces: could not read the calls of {run_id}: {' '.join(str(exc).split())}", file=sys.stderr)
        return
    calls: dict[str, list[dict[str, Any]]] = {}
    files: list[Path] = []
    day = ""
    for row in rows:
        path = _trace_path(row["detail_json"])
        if path is None or not path.is_file():
            continue
        try:
            calls[str(row["call_id"])] = _read_events(path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"traces: could not compact {path}: {' '.join(str(exc).split())}", file=sys.stderr)
            continue
        files.append(path)
        day = day or str(row["ts"])[:10]
    try:
        if not calls:
            return
        if not have_pyarrow():
            print("traces: not compacted, writing traces as Parquet needs pyarrow: install the traces extra", file=sys.stderr)
            return
        try:
            root = resolve_traces_root(traces_url, runs_dir, os.environ)
            written = store_traces.write_run(root, day, run_id, calls)
        except store_traces.ParquetUnavailable as exc:
            print(f"traces: not compacted, {exc}", file=sys.stderr)
            return
        except Exception as exc:
            reason = " ".join(str(exc).split())
            shown = reason.replace(traces_url, redact_url(traces_url)) if traces_url else reason
            print(f"traces: could not compact {run_id}: {shown}", file=sys.stderr)
            return
        expected = sum(len(events) for events in calls.values())
        if written != expected:
            print(f"traces: not compacted, {run_id} wrote {written} rows for {expected} events", file=sys.stderr)
            return
        for path in files:
            try:
                path.unlink()
            except OSError as exc:
                print(f"traces: could not remove {path}: {' '.join(str(exc).split())}", file=sys.stderr)
    finally:
        with contextlib.suppress(OSError):
            (runs_dir / f"{run_id}-trace").rmdir()  # only succeeds on an empty directory


def _usage_line(summary: Mapping[str, Any] | None, models: Sequence[Mapping[str, Any]]) -> str | None:
    """The run's totals as one log line; None when the run recorded no calls.

    `models` are cost_by_model rows, one per (alias, tier); the line has one entry per alias, as the file ledger had.
    """
    if summary is None or not summary["calls"]:
        return None
    aliases = list(dict.fromkeys(m["model_alias"] for m in models))
    merged = [
        (a, sum(m["calls"] for m in models if m["model_alias"] == a), sum(m["cost_usd"] for m in models if m["model_alias"] == a))
        for a in aliases
    ]
    breakdown = ", ".join(f"{a}: {n} call(s) ${round(cost, 4)}" for a, n, cost in merged)
    cached, total = summary["cache_read_tokens"], summary["input_total"]
    share = f", {100 * cached // total}% of input was cache reads" if total else ""
    return f"  usage   : {summary['calls']} node call(s), {summary['turns']} turns, ${round(summary['cost_usd'], 4)} — {breakdown}{share}"


def _undercount_note(run_id: str, stored: int, seen: int) -> str | None:
    """A warning when the runner made more calls than the store holds; a runner's failed store write only logs."""
    return f"store: holds {stored} of the {seen} call(s) {run_id} made; the usage totals undercount" if seen > stored else None


def _print_usage(store: Store, run_id: str, runner: Any) -> None:
    """Print the run's totals from the store. A store that fails here warns; it never changes the run's exit."""
    try:
        summary = run_summary(store.conn, run_id)
        models = cost_by_model(store.conn, run_id)
    except Exception as exc:
        print(f"store: could not read the usage of {run_id}: {' '.join(str(exc).split())}", file=sys.stderr)
        return
    line = _usage_line(summary, models)
    if line is not None:
        print(line)
    note = _undercount_note(run_id, summary["calls"] if summary else 0, len(getattr(runner, "calls", None) or []))
    if note is not None:
        print(note, file=sys.stderr)


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
    from harness.cos import stranded_count

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
        "stranded": stranded_count(),
    }


def _read_overlay(repo: str | None) -> Any | None:
    """Parsed `<repo>/.agent/cartridge.yaml`, or None when repo or the file is absent."""
    if not repo or not Path(overlay_path(repo)).is_file():
        return None
    return yaml.safe_load(Path(overlay_path(repo)).read_text(encoding="utf-8"))


def _lifecycle_worktree(args: argparse.Namespace, cartridge: Mapping[str, Any], run_id: str) -> Path:
    """Where the lifecycle graph's own worktree lives: `<worktree_root>/<run_id>`.

    The one formula `_run_graph`'s check arm and its post-gate apply arm both
    need to create the worktree, and `main`'s exit-time cleanup needs to find
    the same directory again — kept here once so creation and cleanup can
    never compute two different paths for the same run.
    """
    root = args.worktree_root or (cartridge.get("landing_areas") or {}).get("worktree_root", "~/worktrees")
    return Path(str(root)).expanduser() / run_id


def _child_pids(tasks: Path = Path("/proc/self/task")) -> list[int]:
    """This process's direct children, from every thread's Linux `children` file; empty where /proc has none."""

    def read(path: Path) -> str:
        try:
            return path.read_text(encoding="ascii")
        except OSError:
            return ""

    return sorted({int(pid) for path in tasks.glob("*/children") for pid in read(path).split()})


def _terminate_children() -> None:
    for pid in _child_pids():
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGTERM)


def _workers_alive() -> bool:
    return any(t.is_alive() and not t.daemon for t in threading.enumerate() if t is not threading.main_thread())


def _exit_on_sigterm(signum: int, frame: object) -> None:
    """Ignore further SIGTERMs, stop in-flight nodes, and exit 143 (128 + SIGTERM) through `_main`'s `finally`."""
    # Ignored first, so a supervisor's repeat SIGTERM cannot cut the `finally` that stamps `ended_at`.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    # Fan-out nodes run `subprocess.run` in worker threads, which never see this exception,
    # so their children are stopped here rather than by `subprocess.run`'s own cleanup.
    _terminate_children()
    raise SystemExit(143)


def _terminated(exc: BaseException) -> bool:
    """True for the exit `_exit_on_sigterm` raises."""
    return isinstance(exc, SystemExit) and exc.code == 143


def _stop_children_until_quiet(deadline_s: float = 30.0, poll_s: float = 0.1) -> None:
    """Keep stopping children until no worker thread is left, so a node retry cannot outlive the run."""
    end = time.monotonic() + deadline_s
    while _workers_alive() and time.monotonic() < end:
        _terminate_children()
        time.sleep(poll_s)


def main(argv: list[str] | None = None) -> int:
    # Python's default SIGTERM action is to die without running any `finally`,
    # which would leave the run's `ended_at` unset. Only the main thread may
    # install a handler; the previous one is put back on every way out.
    if threading.current_thread() is not threading.main_thread():
        return _main(argv)
    previous = signal.signal(signal.SIGTERM, _exit_on_sigterm)
    try:
        return _main(argv)
    except SystemExit as exc:
        # `ended_at` is already stamped; interpreter exit would otherwise wait on fan-out workers.
        if exc.code == 143:
            _stop_children_until_quiet()
        raise
    finally:
        signal.signal(signal.SIGTERM, previous)


def _main(argv: list[str] | None) -> int:
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

    schema_status = _core_schema_status(core.SCHEMA_VERSION, CORE_SCHEMA)
    if schema_status is not None:
        level, message = schema_status
        print(message, file=sys.stderr if level == "fatal" else sys.stdout)
        if level == "fatal":
            return 1

    run_id = args.run_id or f"{args.graph}-{args.date}-{uuid.uuid4().hex[:8]}"

    # The run's record is opened before any runner exists: a run whose record
    # cannot be kept must not start. `now` is read here, at the edge, and handed down.
    started_at = datetime.now(UTC).isoformat()
    host = socket.gethostname()
    store = _begin_store_run(args, specs, cartridge, run_id, started_at, host)
    if store is None:
        return 1

    # One live run per prefix. A refusal is recorded as this run's end and stops it before any graph.
    name = run_lease.lease_name(run_id)
    try:
        lease = acquire(store.conn, name, run_id, started_at, _LEASE_TTL)
    except Exception as exc:
        print(f"lease: cannot acquire {name}: {' '.join(str(exc).split())}", file=sys.stderr)
        return _refuse_start(store, run_id, "error", 1)
    if not lease.ok:
        print(f"run: {lease.holder} holds {name}; refusing to start {run_id}", file=sys.stderr)
        return _refuse_start(store, run_id, "refused", 2)

    # The heartbeat renews on its own connection from the moment the lease is held, so a slow
    # setup cannot eat the ttl. The epoch fence, not this thread, stops a run that lost the lease.
    heartbeat = run_lease.Heartbeat(
        _storage_url(_read_profile(args.provider_profile), args.runs_dir),
        name,
        run_id,
        lease.epoch,
        clock=lambda: datetime.now(UTC).isoformat(),
    )

    # Setup can raise, or take a SIGTERM, between the lease and the run's own `finally`. That exit
    # still stamps the run and frees the lease, or a relaunch would be refused by a run that is gone.
    try:
        heartbeat.start()
        runner = build_runner(
            scripted=args.scripted,
            provider_profile=args.provider_profile,
            role_skills=role_skill_bodies(cartridge, skill_index),
            workdir=args.workdir,
            repo=args.repo,
        )

        # A runner that still keeps a per-call ledger needs to know where and under
        # what name. Nothing here reads that ledger any more; usage comes from the store.
        if hasattr(runner, "runs_dir"):
            runner.runs_dir = Path(args.runs_dir)
        if hasattr(runner, "run_id"):
            runner.run_id = run_id
        if hasattr(runner, "store"):
            runner.store = store
        if hasattr(runner, "node_cap_usd"):
            runner.node_cap_usd = args.node_cap_usd

        # A runner whose nodes can read the world gets a tool-computed map of it
        # first, so no node pays turns to draw one. The epic driver refreshes it per
        # phase; this is the single-graph case.
        if args.repo and hasattr(runner, "repo_digest"):
            runner.repo_digest = build_digest(Path(args.repo)) or None
        if hasattr(runner, "check_commands"):
            checks = (cartridge.get("landing_areas") or {}).get("checks") or []
            runner.check_commands = [str(c.get("cmd")) for c in checks if isinstance(c, dict) and c.get("cmd")]

        # `lifecycle` is the only graph this file ever creates a worktree for
        # (the check arm and the post-gate apply arm inside `_run_graph`, both via
        # `_lifecycle_worktree`); `epic`, `phase` and `cos` never reach that code,
        # so there is nothing here for them to clean up.
        worktree = _lifecycle_worktree(args, cartridge, run_id) if args.graph == "lifecycle" else None
    except BaseException as exc:
        _finish_store_run(store, run_id, datetime.now(UTC).isoformat(), "error")
        if _terminated(exc):
            _stop_children_until_quiet()
        _end_lease(store, heartbeat, name, run_id, lease.epoch)
        store.conn.close()
        raise

    # Anything that leaves `_run_graph` without returning (a parser error, a raise) is "error".
    status = "error"
    # Set only by a SIGTERM exit. The edge's one flag: the `finally` must quiet the workers before it frees the lease.
    terminated = False
    try:
        code = _run_graph(
            specs=specs,
            parser=parser,
            args=args,
            cartridge=cartridge,
            runner=runner,
            run_id=run_id,
            store=store,
            epoch=lease.epoch,
            lease_name=name,
        )
        status = "ok" if code == 0 else "failed"
        return code
    except SystemExit as exc:
        terminated = _terminated(exc)
        raise
    finally:
        # First, so the run's end is on record even if a later step here raises.
        _finish_store_run(store, run_id, datetime.now(UTC).isoformat(), status)
        # On SIGTERM the prefix stays held until the run's own workers are quiet, so a
        # successor never starts beside them. `main` repeats this wait; by then it finds none.
        if terminated:
            _stop_children_until_quiet()
        _end_lease(store, heartbeat, name, run_id, lease.epoch)
        # Every exit below — success, a caught exception's `return 1`, or
        # anything left to raise past this point — prints the run's totals from
        # the store, which holds every call recorded so far. Guarded like
        # `_finish_store_run`: a store fault warns and never skips what follows.
        # `close` runs AFTER, the order the single-graph path always had: the
        # runner's `calls` must still be readable for the undercount check.
        _print_usage(store, run_id, runner)
        close = getattr(runner, "close", None)
        if callable(close):
            close()
        # Compaction only reads the store and moves files, so it never changes the exit code.
        try:
            _compact_traces(
                store,
                run_id,
                Path(args.runs_dir),
                _traces_url(_read_profile(args.provider_profile), args.runs_dir),
            )
        except Exception as exc:
            print(f"traces: compaction failed: {' '.join(str(exc).split())}", file=sys.stderr)
        store.conn.close()
        # Cleanup runs last — quarantine, a caught exception's `return 1`, or
        # anything still raising past this point — so a run never leaves its
        # worktree behind for a human to notice. Guarded on the directory
        # actually existing: a lifecycle run whose build produced no patch,
        # or whose patch never reached an approved gate decision, never
        # created one, and cleaning up a path that was never made would
        # report a false "removed" or "FAILED to keep" for a run that did
        # nothing wrong. `repo` is --repo when the worktree is a real `git
        # worktree` of it (the check arm's `create_worktree`); when --repo
        # was never given, `worktree` is its own standalone `git init` repo
        # (the scratch dir `apply_patch` falls back to), never a linked
        # worktree of this harness's own checkout, so `worktree` itself — not
        # REPO_ROOT — is the only repo the cleanup call could mean there.
        if worktree is not None and worktree.exists():
            repo = Path(args.repo) if args.repo else worktree
            if args.keep_worktrees:
                ok, detail = keep_worktree(repo, worktree, worktree.parent, run_id)
                print(f"worktree {'kept' if ok else 'FAILED to keep'}: {detail}", file=sys.stderr if not ok else sys.stdout)
            else:
                ok, detail = remove_worktree(repo, worktree)
                print(f"worktree {'removed' if ok else 'FAILED to remove'}: {detail}", file=sys.stderr if not ok else sys.stdout)


def _record_run_to_store(
    store: Store,
    manifest: Mapping[str, Any],
    diffs: Sequence[Mapping[str, Any]],
    *,
    graph_name: str,
    ledger_path: Path | str,
) -> None:
    """Record a run as the epic driver records a phase: one phases row, its gate decisions, its ledger rows.

    A store error warns and never changes the exit code.
    """
    run_id = str(manifest["run_id"])
    phase_run_id = f"{run_id}:{graph_name}"
    row = {
        "run_id": phase_run_id,
        "phase": graph_name,
        "ts": manifest["ts"],
        "principal": manifest["principal"],
        "human_minutes": manifest["human_minutes"],
        "totals": manifest["totals"],
        "manifest": phase_run_id,
        "manifest_record": dict(manifest),
    }
    try:
        for entry in ledger.read(ledger_path):
            if entry.get("run_id") == run_id:
                store.record_ledger(entry)
        store.record_gate_decisions(run_id, graph_name, diffs)
        store.record_phase(row)
    except Exception as exc:
        print(f"store: could not record {run_id}: {' '.join(str(exc).split())}", file=sys.stderr)


def _run_graph(
    *,
    specs: dict[str, GraphSpec],
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
    cartridge: Mapping[str, Any],
    runner: Any,
    run_id: str,
    store: Store | None = None,
    epoch: int | None = None,
    lease_name: str | None = None,
) -> int:
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
            provider_profile=_provider_profile_scope(args.provider_profile),
            runs_dir=args.runs_dir,
            worktree_root=args.worktree_root
            or (cartridge.get("landing_areas") or {}).get("worktree_root", "~/worktrees"),
            assume=args.assume,
            fix_attempts=args.fix_attempts,
            resume_from=args.resume_from,
            store=store,
            epoch=epoch,
            lease_name=lease_name,
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
        print(f"  phases   : recorded in the run store under {run_id}")
        print(f"  ledger   : {args.ledger}")
        for line in result.get("exit_summary") or []:
            print(f"  {line}", file=sys.stderr)
        return 0

    if args.graph == "sweep" and "sweep" not in specs:
        # Listed in `_build_parser`'s choices (docs/design/work-shape.md §1) so
        # the coxswain and this CLI agree the name exists, but
        # graphs/ops/sweep.py declares no SPEC yet, so it never lands in
        # `specs`. Refuse here, the same way `parser.error` refuses a missing
        # `--initiative` below, rather than let `specs[args.graph]` further
        # down raise a bare `KeyError` for a choice argparse just accepted.
        # Gated on `specs` (not just the literal name) so this refusal turns
        # itself off the day a later task gives sweep a real SPEC, instead of
        # outliving its own reason and blocking a working graph forever.
        parser.error("sweep is registered but has no SPEC yet (graphs/ops/sweep.py); not runnable standalone")

    # Set only by the generic single-graph branch below; the phase and cos
    # drivers build a synthetic result and do not honour --result-out.
    result_out: str | None = None

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

        # States of tasks in other initiatives, from the same load as the items:
        # a need on one is met only through this mapping.
        foreign = initiative.get("foreign") or {}
        phase_name = args.phase_name
        if phase_name is None:
            phase_name = next(
                (
                    p
                    for p in initiative["phases"]
                    if workstore.ready_tasks(initiative["items"], phase=p, foreign=foreign)
                ),
                None,
            )
        ready = workstore.ready_tasks(initiative["items"], phase=phase_name, foreign=foreign) if phase_name else []
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
        graph_name = _PHASE_PRINCIPAL
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
        graph_name = _COS_PRINCIPAL
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
        result_out = getattr(args, "result_out", None)
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

    provider_profile = _provider_profile_scope(args.provider_profile)
    proposals = result.get("proposals", [])

    # The check arm. Only when --repo names the project this change targets,
    # and only for the single-graph lifecycle path — the epic driver arriving
    # separately owns fanning this out across a phase's per-task results. It
    # runs BEFORE the policy and the gate see anything: evidence attached
    # after the decision is already made is decoration, not evidence.
    if args.repo and args.graph == "lifecycle" and result.get("build", {}).get("patch"):
        worktree = _lifecycle_worktree(args, cartridge, run_id)
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
            worktree = _lifecycle_worktree(args, cartridge, run_id)
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
    append_ledger(manifest, ledger_path=args.ledger)
    if store is not None:
        _record_run_to_store(store, manifest, diffs, graph_name=graph_name, ledger_path=args.ledger)

    if result_out:
        Path(result_out).parent.mkdir(parents=True, exist_ok=True)
        Path(result_out).write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")

    # And then what the run itself established, on the same clock as the
    # manifest. This lands AFTER the manifest is recorded because it is a post-hoc verdict on
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
    print(f"  manifest: recorded in the run store under {run_id}")
    if result_out:
        print(f"  result  : {result_out}")
    print(f"  ledger  : {args.ledger}")
    return 0
