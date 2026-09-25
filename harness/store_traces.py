"""Trace store: one model call's JSON events, per run, zstd compressed.

Layout under a trace root: YYYY/MM/DD/<run_id>.parquet, one zstd Parquet file per run,
rewritten whole by write_run. Legacy YYYY/MM/DD/<run_id>.jsonl.zst day files still read
when a run has no Parquet file: each append is one zstd frame, and concatenated frames
are a valid zstd stream.
"""

from __future__ import annotations

import io
import json
import os
import sys
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO

from harness.trace_columns import COLUMNS, events_of, to_rows
from harness.traces_url import TracesRoot, have_pyarrow, resolve_traces_root

SUFFIX = ".jsonl.zst"
PARQUET_SUFFIX = ".parquet"


class TracesUnavailable(RuntimeError):
    """zstandard is not installed."""


class ParquetUnavailable(RuntimeError):
    """pyarrow is not installed, so a run cannot be written as Parquet."""


def day_dir(root: Path, day: str) -> Path:
    year, month, date = day.split("-")
    return Path(root) / f"{int(year):04d}" / f"{int(month):02d}" / f"{int(date):02d}"


def run_file(root: Path, day: str, run_id: str) -> Path:
    return day_dir(root, day) / f"{run_id}{SUFFIX}"


def run_parquet(root_path: str, day: str, run_id: str) -> str:
    year, month, date = day.split("-")
    return f"{root_path.rstrip('/')}/{int(year):04d}/{int(month):02d}/{int(date):02d}/{run_id}{PARQUET_SUFFIX}"


def to_lines(run_id: str, call_id: str, events: Sequence[dict[str, Any]]) -> list[str]:
    """One JSONL row per event. seq counts from 0 in event order."""
    return [
        json.dumps({"run_id": run_id, "call_id": call_id, "seq": seq, "event": event}, sort_keys=True, separators=(",", ":"))
        for seq, event in enumerate(events)
    ]


def _zstd() -> Any:
    try:
        import zstandard
    except ImportError as exc:
        raise TracesUnavailable("reading or writing traces needs zstandard: install the traces extra") from exc
    return zstandard


def append_call(root: Path, day: str, run_id: str, call_id: str, events: Sequence[dict[str, Any]]) -> Path:
    zstd = _zstd()
    path = run_file(root, day, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = zstd.ZstdCompressor().compress(("\n".join(to_lines(run_id, call_id, events)) + "\n").encode())
    with path.open("ab") as fh:
        fh.write(frame)
    return path


def read_legacy_file(path: Path) -> Iterator[dict[str, Any]]:
    """Rows of one legacy day file in append order. `event` is a dict."""
    with Path(path).open("rb") as fh:
        reader = _zstd().ZstdDecompressor().stream_reader(fh, read_across_frames=True)
        for line in io.TextIOWrapper(reader, encoding="utf-8"):
            if line.strip():
                yield json.loads(line)


def _as_root(root: str | Path | TracesRoot) -> TracesRoot | None:
    """A plain path becomes a local root. None when pyarrow is missing, so there is no filesystem to build."""
    if isinstance(root, TracesRoot):
        return root
    return resolve_traces_root(str(root), Path("."), {}) if have_pyarrow() else None


def _legacy_dir(root: str | Path | TracesRoot) -> Path | None:
    """The directory legacy files live under. Local roots only."""
    if not isinstance(root, TracesRoot):
        return Path(root)
    from pyarrow.fs import LocalFileSystem

    return Path(root.path) if isinstance(root.fs, LocalFileSystem) else None


def _find_parquet(root: TracesRoot, run_id: str) -> list[str]:
    """Paths of <root>/YYYY/MM/DD/<run_id>.parquet, in date order."""
    from pyarrow.fs import FileSelector, FileType

    depth = len(root.path.strip("/").split("/")) + 4
    name = f"{run_id}{PARQUET_SUFFIX}"
    infos = root.fs.get_file_info(FileSelector(root.path, recursive=True, allow_not_found=True))
    return sorted(
        info.path
        for info in infos
        if info.type == FileType.File
        and len(parts := info.path.strip("/").split("/")) == depth
        and parts[-1] == name
        and all(part.isdigit() for part in parts[-4:-1])
    )


def _parquet_rows(root: TracesRoot, path: str) -> list[dict[str, Any]]:
    import pyarrow.parquet as pq

    with root.fs.open_input_file(path) as fh:
        return pq.ParquetFile(fh).read().to_pylist()


def _from_parquet(root: str | Path | TracesRoot, run_id: str) -> list[dict[str, Any]] | None:
    """Every Parquet row of a run, or None when the run has no Parquet file."""
    resolved = _as_root(root)
    paths = _find_parquet(resolved, run_id) if resolved is not None else []
    return [row for path in paths for row in _parquet_rows(resolved, path)] if paths else None


def _legacy_rows(root: str | Path | TracesRoot, run_id: str) -> Iterator[dict[str, Any]]:
    base = _legacy_dir(root)
    for path in sorted(base.glob(f"*/*/*/{run_id}{SUFFIX}")) if base is not None else []:
        yield from read_legacy_file(path)


def iter_run(root: str | Path | TracesRoot, run_id: str) -> Iterator[dict[str, Any]]:
    """Every row of a run: Parquet if the run has a file, else legacy day files in date order.

    Parquet rows carry every trace_columns column with `event` a JSON string. Legacy rows carry
    run_id, call_id, seq and `event` as a dict.
    """
    rows = _from_parquet(root, run_id)
    yield from rows if rows is not None else _legacy_rows(root, run_id)


def read_call(root: str | Path | TracesRoot, run_id: str, call_id: str) -> list[dict[str, Any]]:
    rows = _from_parquet(root, run_id)
    if rows is not None:
        return events_of([r for r in rows if r["call_id"] == call_id])
    legacy = sorted((r for r in _legacy_rows(root, run_id) if r["call_id"] == call_id), key=lambda r: r["seq"])
    return [r["event"] for r in legacy]


def _table(rows: list[dict[str, Any]]) -> Any:
    """Arrow table of COLUMNS with rows in call_id, seq order. seq is int32."""
    import pyarrow as pa

    types = {"string": pa.string(), "int32": pa.int32()}
    schema = pa.schema([(name, types[kind]) for name, kind in COLUMNS])
    return pa.Table.from_pylist(sorted(rows, key=lambda r: (r["call_id"], r["seq"])), schema=schema)


def _put(root: TracesRoot, path: str, table: Any) -> None:
    import pyarrow.parquet as pq
    from pyarrow.fs import LocalFileSystem

    if isinstance(root.fs, LocalFileSystem):
        root.fs.create_dir(path.rpartition("/")[0], recursive=True)
        tmp = f"{path}.tmp"
        pq.write_table(table, tmp, compression="zstd", filesystem=root.fs)
        root.fs.move(tmp, path)
    else:
        pq.write_table(table, path, compression="zstd", filesystem=root.fs)


def write_run(root: TracesRoot, day: str, run_id: str, calls: Mapping[str, list[dict[str, Any]]]) -> int:
    """Write the run as one Parquet file; return the rows read back for `calls`, not the file total. A call already in the file is replaced."""
    try:
        import pyarrow  # noqa: F401
    except ImportError as exc:
        raise ParquetUnavailable("writing traces as Parquet needs pyarrow: install the traces extra") from exc
    from pyarrow.fs import FileType

    path = run_parquet(root.path, day, run_id)
    kept = _parquet_rows(root, path) if root.fs.get_file_info(path).type == FileType.File else []
    new = [row for call_id, events in calls.items() for row in to_rows(run_id, call_id, day, events)]
    table = _table([r for r in kept if r["call_id"] not in calls] + new)
    _put(root, path, table)
    return sum(1 for r in _parquet_rows(root, path) if r["call_id"] in calls)


def parse_argv(argv: Sequence[str]) -> tuple[str, str] | None:
    """(root, run_id) for exactly `dump <root> <run_id>`, else None."""
    return (argv[1], argv[2]) if len(argv) == 3 and argv[0] == "dump" else None


def dump_lines(rows: list[dict[str, Any]]) -> list[str]:
    """One JSON object per row in call_id, seq order. Keys follow COLUMNS; `event` stays a JSON string."""
    names = [name for name, _ in COLUMNS]
    ordered = sorted(rows, key=lambda r: (r["call_id"], r["seq"]))
    return [json.dumps({name: row[name] for name in names}, ensure_ascii=False) for row in ordered]


def main(argv: Sequence[str], env: Mapping[str, str], out: TextIO, err: TextIO) -> int:
    """Exit 0 rows printed, 2 bad arguments or no pyarrow, 3 no Parquet file, 1 unreadable. Errors never print the root."""
    parsed = parse_argv(argv)
    if parsed is None:
        err.write("usage: python -m harness.store_traces dump <root> <run_id>\n")
        return 2
    root, run_id = parsed
    if not have_pyarrow():
        err.write("the traces extra is not installed\n")
        return 2
    try:
        resolved = resolve_traces_root(root, Path("."), env)
    except ValueError:
        err.write(f"cannot resolve the traces root for run {run_id}\n")
        return 2
    try:
        rows = _from_parquet(resolved, run_id)
    except OSError:
        err.write(f"cannot read traces for run {run_id}\n")
        return 1
    if rows is None:
        return 3
    out.write("".join(f"{line}\n" for line in dump_lines(rows)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:], os.environ, sys.stdout, sys.stderr))
