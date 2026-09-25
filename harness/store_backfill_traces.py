"""Repack traces into one Parquet file per run under a trace store root.

Usage: python -m harness.store_backfill_traces TRACES_ROOT [--loose-root DIR] [--calls-dir DIR]
       python -m harness.store_backfill_traces relink TRACES_ROOT [--store-url URL] [--dry-run]

Two sources feed a run. LOOSE_ROOT holds <run_id>-trace/<role>-<n>.jsonl, one stream event per
line and no timestamps. Day and call id come from CALLS_DIR/<run_id>.calls.jsonl, matched on the
run and the final file name of each call's "trace" key. A trace with no call is imported as
<run_id>-<role>-<n>, dated by file modification time. TRACES_ROOT may also hold legacy
YYYY/MM/DD/<run_id>.jsonl.zst day files. Both sources of one run merge into one Parquet file,
dated by the earliest source day, or by the day of a Parquet file the run already has.

Sources move to TRACES_ROOT/archive/, keeping their YYYY/MM/DD path, only after the Parquet file
reads back with the same event count for every call. Readers list YYYY/MM/DD only, so archive/ is
never read. Nothing is ever deleted. On a mismatch the sources stay and the Parquet file is kept.
A call whose events differ between two sources is a conflict: nothing is written or moved.

Wrong belief to avoid: "write_run replaces by call_id, so a rerun is safe". It replaces only inside
the one file at its day path, and readers join every day's file for a run. A rerun after a partial
archive would date the run by the sources left and write a second file, doubling every call. So an
existing Parquet file pins the day.

An empty trace file writes no rows. It verifies at zero events and is archived with its run, so a
rerun finds nothing left to do.

`relink` rewrites call ids in Parquet files that carry the synthetic id <run_id>-<role>-<n> to the
`legacy:` id the store holds for the same call, matched on the call's "trace" file name. Wrong belief
to avoid: "the synthetic id is the id the store knows". Only a call imported without an id has a
`legacy:` id, and the backfill above cannot know it, so the two never join until relink runs.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import reduce
from pathlib import Path
from typing import Any

from harness import store_read, store_traces
from harness.store_dialect import default_url, json_load
from harness.traces_url import TracesRoot, have_pyarrow, redact_url, resolve_traces_root

TRACE_DIR_SUFFIX = "-trace"
CALLS_SUFFIX = ".calls.jsonl"
ARCHIVE_DIR = "archive"
LEGACY_PREFIX = "legacy:"
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
class Source:
    path: Path
    archive_rel: Path


@dataclass(frozen=True)
class RunSources:
    """Everything one run will write. `expected` is the event count each call must read back with."""

    run_id: str
    day: str
    calls: dict[str, list[dict[str, Any]]]
    expected: dict[str, int]
    sources: tuple[Source, ...]
    conflicts: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Mismatch:
    run_id: str
    expected: int
    got: int


@dataclass(frozen=True)
class Conflict:
    run_id: str
    call_ids: tuple[str, ...]


@dataclass(frozen=True)
class Report:
    runs: int = 0
    sources: int = 0
    empty: int = 0
    rows: int = 0
    archived: int = 0
    blocked: int = 0
    src_bytes: int = 0
    dst_bytes: int = 0
    mismatches: tuple[Mismatch, ...] = ()
    conflicts: tuple[Conflict, ...] = ()
    unavailable: bool = False


@dataclass(frozen=True)
class RelinkReport:
    """`changed` is (run_id, rows) per run with rows to relink; rows are counted even when dry."""

    changed: tuple[tuple[str, int], ...] = ()
    mismatches: tuple[Mismatch, ...] = ()


@dataclass(frozen=True)
class RunResult:
    run: RunSources
    rows: int
    src_bytes: int
    dst_bytes: int
    archived: int
    blocked: int
    mismatch: Mismatch | None


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
    """Rows without an `id` (written before calls carried one) are skipped: their traces match no call and fall back to the file's day."""
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
        if "id" in row and "ts" in row
    ]


def find_trace_files(loose_root: Path) -> list[TraceFile]:
    return [
        TraceFile(d.name[: -len(TRACE_DIR_SUFFIX)], parsed[0], parsed[1], f)
        for d in sorted(Path(loose_root).glob(f"*{TRACE_DIR_SUFFIX}"))
        if d.is_dir()
        for f in sorted(d.glob("*.jsonl"))
        if (parsed := parse_trace_name(f.name))
    ]


def _dated(root: Path, name: str) -> list[Path]:
    """Files <root>/YYYY/MM/DD/<name> in date order. archive/ and stray directories never match."""
    return [
        path
        for path in sorted(Path(root).glob(f"*/*/*/{name}"))
        if all(part.isdigit() for part in path.relative_to(root).parts[:3])
    ]


def find_legacy_files(root: Path) -> list[Path]:
    return _dated(root, f"*{store_traces.SUFFIX}")


def parquet_days(root: Path, run_id: str) -> list[str]:
    """Days that already hold <run_id>.parquet, earliest first."""
    return ["-".join(p.relative_to(root).parts[:3]) for p in _dated(root, f"{run_id}{store_traces.PARQUET_SUFFIX}")]


def _json_lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def read_events(path: Path) -> list[dict[str, Any]]:
    return _json_lines(path)


def loose_run(move: Move, path: Path, events: list[dict[str, Any]]) -> RunSources:
    """An empty file contributes no call but still expects zero events, so it archives with its run."""
    rel = store_traces.day_dir(Path(), move.day) / f"{move.run_id}{TRACE_DIR_SUFFIX}" / path.name
    calls = {move.call_id: events} if events else {}
    return RunSources(move.run_id, move.day, calls, {move.call_id: len(events)}, (Source(path, rel),))


def legacy_run(path: Path, rows: Sequence[dict[str, Any]]) -> RunSources:
    """One legacy day file. Its date is the directory it sits in; each call's events come back in seq order."""
    calls = {
        call_id: [r["event"] for r in sorted((r for r in rows if r["call_id"] == call_id), key=lambda r: r["seq"])]
        for call_id in sorted({r["call_id"] for r in rows})
    }
    rel = Path(*path.parts[-4:])
    return RunSources(
        path.name.removesuffix(store_traces.SUFFIX),
        "-".join(path.parts[-4:-1]),
        calls,
        {call_id: len(events) for call_id, events in calls.items()},
        (Source(path, rel),),
    )


def _combine(a: RunSources, b: RunSources) -> RunSources:
    """A call both hold counts once when its events are equal and is a conflict when they differ."""
    shared = a.calls.keys() & b.calls.keys()
    differ = frozenset(c for c in shared if a.calls[c] != b.calls[c])
    return RunSources(
        a.run_id,
        min(a.day, b.day),
        {**a.calls, **b.calls},
        {
            c: b.expected[c] if c in shared else a.expected.get(c, 0) + b.expected.get(c, 0)
            for c in a.expected.keys() | b.expected.keys()
        },
        a.sources + b.sources,
        a.conflicts | b.conflicts | differ,
    )


def group_by_run(runs: Sequence[RunSources]) -> dict[str, RunSources]:
    return {
        run_id: reduce(_combine, [r for r in runs if r.run_id == run_id]) for run_id in sorted({r.run_id for r in runs})
    }


def _local_dir(root: TracesRoot) -> Path:
    from pyarrow.fs import LocalFileSystem

    if not isinstance(root.fs, LocalFileSystem):
        raise ValueError(f"backfill needs a local traces root, got {redact_url(root.path)}")
    return Path(root.path)


def _archive(base: Path, sources: Sequence[Source]) -> int:
    """Move each source under base/archive/. Moves nothing if any destination is taken. Returns the count moved."""
    dests = [base / ARCHIVE_DIR / s.archive_rel for s in sources]
    if any(d.exists() for d in dests):
        return 0
    for source, dest in zip(sources, dests, strict=True):
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(source.path, dest)
    return len(sources)


def _convert(traces: TracesRoot, base: Path, run: RunSources) -> RunResult:
    src_bytes = sum(s.path.stat().st_size for s in run.sources)
    if run.conflicts:
        return RunResult(run, 0, src_bytes, 0, 0, 0, None)
    day = next(iter(parquet_days(base, run.run_id)), run.day)
    rows = store_traces.write_run(traces, day, run.run_id, run.calls) if run.calls else 0
    seen = Counter(r["call_id"] for r in store_traces.iter_run(traces, run.run_id))
    got = {call_id: seen[call_id] for call_id in run.expected}
    verified = got == run.expected
    moved = _archive(base, run.sources) if verified else 0
    parquet = Path(store_traces.run_parquet(traces.path, day, run.run_id))
    return RunResult(
        run,
        rows,
        src_bytes,
        parquet.stat().st_size if parquet.exists() else 0,
        moved,
        len(run.sources) - moved if verified else 0,
        None if verified else Mismatch(run.run_id, sum(run.expected.values()), sum(got.values())),
    )


def _read_legacy(paths: Sequence[Path]) -> list[tuple[Path, list[dict[str, Any]]]] | None:
    """Rows of every legacy file, or None when zstandard is missing and they cannot be read."""
    try:
        return [(p, list(store_traces.read_legacy_file(p))) for p in paths]
    except store_traces.TracesUnavailable:
        return None


def _unavailable(why: str) -> Report:
    print(f"warning: {why}; nothing converted, every source left in place", file=sys.stderr)
    return Report(unavailable=True)


def backfill(
    root: str | TracesRoot,
    loose_root: Path | None,
    calls_dir: Path | None,
    mtime_day: Callable[[Path], str],
) -> Report:
    """Convert loose traces and legacy day files to one Parquet file per run. A str root resolves with an empty env."""
    if not have_pyarrow():
        return _unavailable("pyarrow is not installed")
    traces = root if isinstance(root, TracesRoot) else resolve_traces_root(root, Path("."), {})
    base = _local_dir(traces)
    legacy = _read_legacy(find_legacy_files(base))
    if legacy is None:
        return _unavailable("zstandard is not installed, so legacy day files cannot be read")
    files = find_trace_files(loose_root) if loose_root is not None else []
    plan = plan_moves(load_calls(calls_dir) if calls_dir is not None else [], files, mtime_day)
    loose = [(plan[f], f.path, read_events(f.path)) for f in files]
    runs = group_by_run([legacy_run(p, rows) for p, rows in legacy] + [loose_run(m, p, ev) for m, p, ev in loose])
    results = [_convert(traces, base, run) for run in runs.values()]
    return Report(
        runs=len(results),
        sources=sum(len(r.run.sources) for r in results),
        empty=sum(1 for _, _, ev in loose if not ev) + sum(1 for _, rows in legacy if not rows),
        rows=sum(r.rows for r in results),
        archived=sum(r.archived for r in results),
        blocked=sum(r.blocked for r in results),
        src_bytes=sum(r.src_bytes for r in results),
        dst_bytes=sum(r.dst_bytes for r in results),
        mismatches=tuple(r.mismatch for r in results if r.mismatch is not None),
        conflicts=tuple(Conflict(r.run.run_id, tuple(sorted(r.run.conflicts))) for r in results if r.run.conflicts),
    )


def file_mtime_day(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, UTC).date().isoformat()


def relink_map(calls: Sequence[Call]) -> dict[str, str]:
    """Synthetic call id to store id, for calls the store imported without an id and that name a trace."""
    return {f"{c.run_id}-{Path(c.trace).stem}": c.id for c in calls if c.id.startswith(LEGACY_PREFIX) and c.trace}


def relink_rows(rows: Sequence[dict[str, Any]], mapping: dict[str, str]) -> tuple[list[dict[str, Any]], int]:
    """Copies of `rows` with call_id mapped, and how many changed."""
    return (
        [{**r, "call_id": mapping.get(r["call_id"], r["call_id"])} for r in rows],
        sum(1 for r in rows if r["call_id"] in mapping),
    )


def load_legacy_calls(store_url: str) -> list[Call]:
    """Store calls with a `legacy:` id whose detail_json names a trace. Reads only; the store is never written."""
    conn = store_read.connect_readonly(store_url)
    try:
        rows = conn.query_all(
            "SELECT run_id, call_id, detail_json FROM node_calls"
            f" WHERE substr(call_id, 1, {len(LEGACY_PREFIX)}) = '{LEGACY_PREFIX}' ORDER BY run_id, seq"
        )
    finally:
        conn.close()
    return [
        Call(run_id, call_id, "", "", str(detail["trace"]))
        for run_id, call_id, raw in rows
        if isinstance(detail := json_load(raw), dict) and detail.get("trace")
    ]


def _relink_run(traces: TracesRoot, run_id: str, mapping: dict[str, str], dry_run: bool) -> tuple[int, list[Mismatch]]:
    """Rows changed across the run's Parquet files. A file is rewritten only when its row count holds."""
    rows_changed, bad = 0, []
    for path in store_traces._find_parquet(traces, run_id):
        rows = store_traces._parquet_rows(traces, path)
        new, n = relink_rows(rows, mapping)
        if n and len(new) != len(rows):
            bad.append(Mismatch(run_id, len(rows), len(new)))
        elif n:
            rows_changed += n
            if not dry_run:
                store_traces._put(traces, path, store_traces._table(new))
    return rows_changed, bad


def relink(traces: TracesRoot, calls: Sequence[Call], dry_run: bool) -> RelinkReport:
    """Rewrite synthetic call ids in each run's Parquet files to the store's `legacy:` ids."""
    runs = sorted({c.run_id for c in calls if c.id.startswith(LEGACY_PREFIX) and c.trace})
    results = [
        (run_id, *_relink_run(traces, run_id, relink_map([c for c in calls if c.run_id == run_id]), dry_run))
        for run_id in runs
    ]
    return RelinkReport(
        changed=tuple((run_id, n) for run_id, n, _ in results if n),
        mismatches=tuple(m for _, _, bad in results for m in bad),
    )


def relink_main(argv: Sequence[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m harness.store_backfill_traces relink",
        description="Rewrite synthetic trace call ids to the store's legacy call ids",
    )
    ap.add_argument("traces_root", help="local trace store root holding the Parquet files")
    ap.add_argument("--store-url", default=None, help="store URL; default cox.db in the parent of traces_root")
    ap.add_argument("--dry-run", action="store_true", help="report what would change and write nothing")
    args = ap.parse_args(argv)
    if not have_pyarrow():
        print("error: pyarrow is not installed", file=sys.stderr)
        return 2
    try:
        traces = resolve_traces_root(args.traces_root, Path("."), {})
        _local_dir(traces)
        calls = load_legacy_calls(args.store_url or default_url(Path(args.traces_root).resolve().parent))
        report = relink(traces, calls, args.dry_run)
    except (ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    verb = "would relink" if args.dry_run else "relinked"
    for run_id, n in report.changed:
        print(f"{verb} run {run_id}: {n} rows")
    for m in report.mismatches:
        print(f"mismatch: run {m.run_id} had {m.expected} rows, relink produced {m.got}; nothing written")
    rows = sum(n for _, n in report.changed)
    print(f"{verb}: {len(report.changed)} runs, {rows} rows" if report.changed else "nothing to change")
    return 1 if report.mismatches else 0


def main(argv: Sequence[str] | None = None) -> int:
    given = sys.argv[1:] if argv is None else list(argv)
    if given[:1] == ["relink"]:
        return relink_main(given[1:])
    ap = argparse.ArgumentParser(prog="python -m harness.store_backfill_traces", description=__doc__.splitlines()[0])
    ap.add_argument("traces_root", help="local trace store root; Parquet is written here and sources archived under it")
    ap.add_argument("--loose-root", type=Path, default=None, help="directory of <run_id>-trace directories")
    ap.add_argument("--calls-dir", type=Path, default=None, help="directory of <run_id>.calls.jsonl files")
    args = ap.parse_args(given)
    try:
        report = backfill(args.traces_root, args.loose_root, args.calls_dir, file_mtime_day)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"traces_root: {redact_url(args.traces_root)}")
    for name, value in vars(report).items():
        if name not in ("mismatches", "conflicts"):
            print(f"{name}: {value}")
    for m in report.mismatches:
        print(f"mismatch: run {m.run_id} expected {m.expected} events, read back {m.got}")
    for c in report.conflicts:
        print(f"conflict: run {c.run_id} calls {', '.join(c.call_ids)} differ between sources; nothing written")
    failed = report.mismatches or report.conflicts or report.blocked or report.unavailable
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
