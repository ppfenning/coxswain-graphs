"""Repack loose per-call trace files into the day-partitioned trace store.

Usage: python -m harness.store_backfill_traces LOOSE_ROOT CALLS_DIR NEW_ROOT [--archive DIR]

LOOSE_ROOT holds <run_id>-trace/<role>-<n>.jsonl, one stream event per line and no
timestamps. Day and call id come from CALLS_DIR/<run_id>.calls.jsonl, matched on the
run and the final file name of each call's "trace" key. A trace with no call is
imported as <run_id>-<role>-<n>, dated by file modification time.

Wrong belief to avoid: an empty trace file is not "present". It writes no rows, so a
rerun could never see it. It is counted as seen and left unimported, which fails the run.
Sources are moved to the archive only after they read back with the same event count.
Nothing is ever deleted.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from harness import store_traces

TRACE_DIR_SUFFIX = "-trace"
CALLS_SUFFIX = ".calls.jsonl"
_NAME = re.compile(r"^(?P<role>.+)-(?P<index>\d+)\.jsonl$")


@dataclass(frozen=True)
class TraceFile:
    run_id: str
    role: str
    index: int
    path: Path


@dataclass(frozen=True)
class Call:
    run_id: str
    id: str
    role: str
    ts: str
    trace: str


@dataclass(frozen=True)
class Move:
    day: str
    run_id: str
    call_id: str
    matched: bool


@dataclass(frozen=True)
class Report:
    seen: int
    appended: int
    present: int
    unmatched: int
    archived: int
    src_bytes: int
    dst_bytes: int


def parse_trace_name(name: str) -> tuple[str, int] | None:
    """'build-2.jsonl' is ('build', 2). The index is 1-based."""
    m = _NAME.match(name)
    return (m["role"], int(m["index"])) if m else None


def plan_moves(
    calls: Sequence[Call], trace_files: Sequence[TraceFile], mtime_day: Callable[[Path], str]
) -> dict[TraceFile, Move]:
    by_key = {(c.run_id, c.trace.rsplit("/", 1)[-1]): c for c in calls}

    def move(t: TraceFile) -> Move:
        call = by_key.get((t.run_id, t.path.name))
        if call is None:
            return Move(mtime_day(t.path), t.run_id, f"{t.run_id}-{t.role}-{t.index}", False)
        return Move(call.ts[:10], t.run_id, call.id, True)

    return {t: move(t) for t in trace_files}


def load_calls(calls_dir: Path) -> list[Call]:
    return [
        Call(
            run_id=path.name[: -len(CALLS_SUFFIX)],
            id=str(row["id"]),
            role=str(row.get("role", "")),
            ts=str(row["ts"]),
            trace=str(row.get("trace", "")),
        )
        for path in sorted(Path(calls_dir).glob(f"*{CALLS_SUFFIX}"))
        for row in _json_lines(path)
    ]


def find_trace_files(loose_root: Path) -> list[TraceFile]:
    return [
        TraceFile(d.name[: -len(TRACE_DIR_SUFFIX)], parsed[0], parsed[1], f)
        for d in sorted(Path(loose_root).glob(f"*{TRACE_DIR_SUFFIX}"))
        if d.is_dir()
        for f in sorted(d.glob("*.jsonl"))
        if (parsed := parse_trace_name(f.name))
    ]


def _json_lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def read_events(path: Path) -> list[dict[str, Any]]:
    return _json_lines(path)


def already_present(root: Path, run_id: str, call_id: str) -> bool:
    return bool(store_traces.read_call(root, run_id, call_id))


def _import_one(new_root: Path, move: Move, path: Path) -> tuple[str, int]:
    events = read_events(path)
    if not events:
        return "empty", 0
    if already_present(new_root, move.run_id, move.call_id):
        return "present", len(events)
    store_traces.append_call(new_root, move.day, move.run_id, move.call_id, events)
    return "appended", len(events)


def _archive_one(new_root: Path, archive: Path, move: Move, path: Path, n_events: int) -> bool:
    if len(store_traces.read_call(new_root, move.run_id, move.call_id)) != n_events:
        return False
    dest = archive / path.parent.name / path.name
    if dest.exists():
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(path, dest)
    return True


def import_all(
    loose_root: Path, calls_dir: Path, new_root: Path, archive: Path | None, mtime_day: Callable[[Path], str]
) -> Report:
    files = find_trace_files(loose_root)
    plan = plan_moves(load_calls(calls_dir), files, mtime_day)
    src_bytes = sum(f.path.stat().st_size for f in files)
    outcomes = [(f, plan[f], *_import_one(new_root, plan[f], f.path)) for f in files]
    landed = [(f, m, n) for f, m, outcome, n in outcomes if outcome in ("appended", "present")]
    archived = sum(_archive_one(new_root, archive, m, f.path, n) for f, m, n in landed) if archive is not None else 0
    dst_files = {store_traces.run_file(new_root, m.day, m.run_id) for _, m, _ in landed}
    return Report(
        seen=len(files),
        appended=sum(1 for *_, outcome, _n in outcomes if outcome == "appended"),
        present=sum(1 for *_, outcome, _n in outcomes if outcome == "present"),
        unmatched=sum(1 for _, m, *_ in outcomes if not m.matched),
        archived=archived,
        src_bytes=src_bytes,
        dst_bytes=sum(p.stat().st_size for p in dst_files if p.exists()),
    )


def file_mtime_day(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, UTC).date().isoformat()


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m harness.store_backfill_traces", description=__doc__.splitlines()[0])
    ap.add_argument("loose_root", type=Path, help="directory of <run_id>-trace directories")
    ap.add_argument("calls_dir", type=Path, help="directory of <run_id>.calls.jsonl files")
    ap.add_argument("new_root", type=Path, help="trace store root to write")
    ap.add_argument("--archive", type=Path, default=None, help="move verified sources here; never deletes")
    args = ap.parse_args(argv)
    report = import_all(args.loose_root, args.calls_dir, args.new_root, args.archive, file_mtime_day)
    for name, value in vars(report).items():
        print(f"{name}: {value}")
    return 0 if report.seen == report.appended + report.present else 1


if __name__ == "__main__":
    sys.exit(main())
