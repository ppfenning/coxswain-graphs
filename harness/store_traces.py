"""Trace store: one model call's JSON events, in per-run day files, zstd compressed.

Layout under a trace root: YYYY/MM/DD/<run_id>.jsonl.zst. One file per run per day,
so a run has a single writer and one glob reads the whole tree. Each append is one
zstd frame; concatenated frames are a valid zstd stream.
"""

from __future__ import annotations

import io
import json
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

SUFFIX = ".jsonl.zst"


class TracesUnavailable(RuntimeError):
    """zstandard is not installed."""


def day_dir(root: Path, day: str) -> Path:
    year, month, date = day.split("-")
    return Path(root) / f"{int(year):04d}" / f"{int(month):02d}" / f"{int(date):02d}"


def run_file(root: Path, day: str, run_id: str) -> Path:
    return day_dir(root, day) / f"{run_id}{SUFFIX}"


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


def _rows(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("rb") as fh:
        reader = _zstd().ZstdDecompressor().stream_reader(fh, read_across_frames=True)
        for line in io.TextIOWrapper(reader, encoding="utf-8"):
            if line.strip():
                yield json.loads(line)


def iter_run(root: Path, run_id: str) -> Iterator[dict[str, Any]]:
    """Every row of a run, day directories in date order, rows in append order."""
    for path in sorted(Path(root).glob(f"*/*/*/{run_id}{SUFFIX}")):
        yield from _rows(path)


def read_call(root: Path, run_id: str, call_id: str) -> list[dict[str, Any]]:
    rows = sorted((r for r in iter_run(root, run_id) if r["call_id"] == call_id), key=lambda r: r["seq"])
    return [r["event"] for r in rows]
