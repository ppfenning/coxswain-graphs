"""Copy every table's rows from one store to another, idempotently, and check the counts.

    python -m harness.store_copy SRC_URL DST_URL [--json]

Both sides are opened with `open_store`, so the destination is migrated first. Rows
go in with insert-ignore, so a second run inserts nothing and reports every row
present. A destination row with the same key but different content is left alone.
`schema_version` is not copied: each side migrates itself. The source is only read.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from urllib.parse import urlsplit

import harness.store_ddl_0001 as ddl1
import harness.store_ddl_0002 as ddl2
import harness.store_ddl_0003 as ddl3
import harness.store_ddl_0004 as ddl4
import harness.store_ddl_0005 as ddl5
import harness.store_ddl_0006 as ddl6
from harness.store_dialect import Connection, insert_ignore
from harness.store_migrate import open_store

Table = tuple[str, tuple[str, ...], tuple[str, ...]]

# Parents before children: the registry, then runs, then what hangs off a run.
_ORDER = (
    "graphs",
    "graph_nodes",
    "graph_edges",
    "runs",
    "phases",
    "tasks",
    "task_records",
    "work_items",
    "attempts",
    "node_calls",
    "gate_decisions",
    "ledger",
    "leases",
)


class CopyCheckFailed(Exception):
    """A destination table holds fewer rows than the source had. `report` is the full copy report."""

    def __init__(self, report: dict[str, dict[str, int]], short: dict[str, int]) -> None:
        super().__init__(f"destination is short of the source in: {', '.join(sorted(short))}")
        self.report = report
        self.short = short


def tables() -> tuple[Table, ...]:
    """(name, column names, key) for every table the DDL modules create, migration ALTER columns included."""
    # The list must follow every migration's ALTERs: a column left out is dropped silently and the copy holds NULL.
    alters = (*ddl2._ADDED, *ddl3._ADDED, *ddl5._ADDED)
    added = {t: tuple(c for u, c in alters if u == t) for t, _ in alters}
    return tuple(
        (name, (*(c for c, _ in columns), *added.get(name, ())), key)
        for name, columns, key in (*ddl1._TABLES, *ddl2._TABLES, *ddl4._TABLES, *ddl6._TABLES)
    )


def plan(known: Sequence[Table]) -> tuple[Table, ...]:
    """`known` in copy order. A table the order does not name is an error, so none is dropped silently."""
    unnamed = sorted(name for name, _, _ in known if name not in _ORDER)
    if unnamed:
        raise ValueError(f"no copy order for table(s): {', '.join(unnamed)}")
    by_name = {t[0]: t for t in known}
    return tuple(by_name[n] for n in _ORDER if n in by_name)


def _safe_url(url: str) -> str:
    """Scheme and path only: userinfo, host, port and query never reach output."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return f"{url.partition(':')[0]}://"
    return f"{parts.scheme}://{parts.path}"


def _count(conn: Connection, table: str) -> int:
    row = conn.query_one(f"SELECT COUNT(*) FROM {table}")
    return 0 if row is None else int(row[0])


def _copy_table(
    src: Connection, dst: Connection, table: str, columns: tuple[str, ...], key: tuple[str, ...], batch: int
) -> tuple[int, int]:
    """(rows read from src, rows inserted into dst). Reads in key order, one destination transaction per batch."""
    p = src.dialect.placeholder
    select = f"SELECT {', '.join(columns)} FROM {table} ORDER BY {', '.join(key)} LIMIT {p} OFFSET {p}"
    insert = insert_ignore(dst.dialect, table, columns, key)
    read = inserted = 0
    while True:
        rows = src.query_all(select, (batch, read))
        with dst.transaction():
            inserted += sum(dst.execute(insert, row) for row in rows)
        read += len(rows)
        if len(rows) < batch:
            return read, inserted


def copy(src_url: str, dst_url: str, now: str, batch: int = 500) -> dict[str, dict[str, int]]:
    """Copy every table, then check each destination count is at least the source count.

    Raises CopyCheckFailed, carrying the report, when one is short.
    """
    if batch < 1:
        raise ValueError(f"batch must be at least 1, got {batch}")
    src = open_store(src_url, now)
    try:
        dst = open_store(dst_url, now)
        try:
            report: dict[str, dict[str, int]] = {}
            for name, columns, key in plan(tables()):
                read, inserted = _copy_table(src, dst, name, columns, key, batch)
                report[name] = {"source": read, "copied": inserted, "present": read - inserted}
            held = {n: _count(dst, n) for n in report}
            short = {n: c for n, c in held.items() if c < report[n]["source"]}
        finally:
            dst.close()
    finally:
        src.close()
    if short:
        raise CopyCheckFailed(report, short)
    return report


def _lines(report: dict[str, dict[str, int]]) -> list[str]:
    return [f"{n} source={r['source']} copied={r['copied']} present={r['present']}" for n, r in report.items()]


def main(argv: Sequence[str]) -> int:
    ap = argparse.ArgumentParser(prog="python -m harness.store_copy", description=__doc__.split("\n")[0])
    ap.add_argument("src_url")
    ap.add_argument("dst_url")
    ap.add_argument("--json", action="store_true", help="print one JSON object instead of a line per table")
    args = ap.parse_args(argv)
    src, dst = _safe_url(args.src_url), _safe_url(args.dst_url)
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        report, short = copy(args.src_url, args.dst_url, now), {}
    except CopyCheckFailed as failed:
        report, short = failed.report, failed.short
    except Exception as exc:  # the message may hold the URL; name only the class
        print(f"error: could not copy {src} to {dst}: {type(exc).__name__}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({"from": src, "to": dst, "tables": report, "short": short, "ok": not short}, sort_keys=True))
    else:
        print(f"copy {src} -> {dst}")
        print("\n".join(_lines(report)))
        for n, c in short.items():
            print(f"FAIL {n}: destination has {c}, source has {report[n]['source']}")
    return 1 if short else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
