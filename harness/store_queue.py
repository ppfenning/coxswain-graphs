"""python -m harness.store_queue: read, upsert, claim and release rows on the work_items queue.

The four verbs coxswain-tools already calls, documented in its agent_tools/run_store.py
as "Assumed harness subcommands, for graphs to match". Built on store_dialect's
Connection, placeholder and json helpers, never a hand-rolled dialect branch. Time is
always an argument: claim's NOW and TTL_S come from argv, nothing here reads the clock.

This module owns the queue and claim columns migration 0009 added to work_items: kind,
title, surfaces_json, body, extra_json, holder, epoch, expires_at. The seven columns
store_read.work_items and store_work_state already read and write (initiative, task_id,
phase, state, needs_json, updated_at, updated_by) keep their meaning; this module writes
them the same way those helpers do, through the same table, and never adds a duplicate
notion of what they mean.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from harness.store_dialect import Connection, connect, json_load, json_text
from harness.store_dialect import upsert as upsert_sql

__all__ = ["claim", "read", "release", "upsert"]

EXIT_OK = 0
EXIT_BAD_INPUT = 2
EXIT_CLAIMED = 4

Row = dict[str, Any]

# Every column read/upsert touches, in select order. The claim columns (holder, epoch,
# expires_at) are last: claim and release below address them directly, by name, and
# never through this tuple.
_QUEUE_COLS = (
    "initiative", "task_id", "kind", "phase", "state", "needs_json", "title",
    "surfaces_json", "body", "extra_json", "updated_at", "updated_by",
    "holder", "epoch", "expires_at",
)  # fmt: skip
_RENAME = {"needs_json": "needs", "surfaces_json": "surfaces", "extra_json": "extra"}
# The fields upsert compares to decide whether a repeat changes anything.
_CONTENT = ("kind", "phase", "state", "needs", "title", "surfaces", "body", "extra")
_KEY_COLS = ("initiative", "task_id")


def _decode(cols: Sequence[str], row: Sequence[Any]) -> Row:
    """Column names zipped to a row; needs_json, surfaces_json and extra_json decoded and renamed."""
    return {_RENAME.get(c, c): (json_load(v) if c in _RENAME else v) for c, v in zip(cols, row)}


def read(conn: Connection, initiative: str | None = None, kind: str | None = None) -> list[Row]:
    """Queue rows, optionally filtered by initiative and/or kind, in (initiative, task_id) order."""
    p = conn.dialect.placeholder
    filters = [(col, val) for col, val in (("initiative", initiative), ("kind", kind)) if val is not None]
    where = f" WHERE {' AND '.join(f'{col} = {p}' for col, _ in filters)}" if filters else ""
    sql = f"SELECT {', '.join(_QUEUE_COLS)} FROM work_items{where} ORDER BY initiative, task_id"
    return [_decode(_QUEUE_COLS, r) for r in conn.query_all(sql, [val for _, val in filters])]


def _insert_row(row: Row) -> Row:
    """`row` (read()'s decoded shape) turned into the insert column values upsert writes; holder/epoch/expires_at excluded."""
    return {
        "initiative": row["initiative"],
        "task_id": row["task_id"],
        "kind": row.get("kind"),
        "phase": row.get("phase"),
        "state": row.get("state"),
        "needs_json": row.get("needs"),
        "title": row.get("title"),
        "surfaces_json": row.get("surfaces"),
        "body": row.get("body"),
        "extra_json": row.get("extra"),
        # Every work_items column is NOT NULL (migration 0006); a caller that sends no stamp, as tools' `route import`
        # rows do, gets the write time and this module's name rather than a refused row.
        "updated_at": row.get("updated_at") or datetime.now(UTC).isoformat(),
        "updated_by": row.get("updated_by") or "store_queue",
    }


def _params(values: Row) -> list[Any]:
    return [json_text(v) if c in _RENAME and v is not None else v for c, v in values.items()]


def _current(conn: Connection, initiative: str, task: str) -> Row | None:
    return next((r for r in read(conn, initiative) if r["task_id"] == task), None)


def _unchanged(current: Row | None, row: Row) -> bool:
    """True when the stored row already holds every content field `row` carries; updated_at and updated_by do not count."""
    return current is not None and all(current[k] == row.get(k) for k in _CONTENT)


def upsert(conn: Connection, row: Row) -> bool:
    """Insert a new (initiative, task_id) row, or update its queue columns; False when the content was already stored.

    holder, epoch and expires_at are not in the column list, so the SET never touches a claim.
    """
    values = _insert_row(row)
    with conn.transaction():
        # store_dialect.upsert overwrites every listed column, updated_at included: this guard keeps
        # updated_at stable when a repeat carries the same content.
        if _unchanged(_current(conn, row["initiative"], row["task_id"]), row):
            return False
        conn.execute(upsert_sql(conn.dialect, "work_items", list(values), _KEY_COLS), _params(values))
    return True


def _until(now: str, ttl_s: int) -> str:
    """`now` (YYYY-MM-DDTHH:MM:SSZ) plus ttl_s seconds, in the same fixed form."""
    parsed = datetime.fromisoformat(now.replace("Z", "+00:00"))
    return (parsed + timedelta(seconds=ttl_s)).strftime("%Y-%m-%dT%H:%M:%SZ")


def claim(conn: Connection, initiative: str, task: str, holder: str, ttl_s: int, now: str) -> Row | None:
    """Claim (initiative, task) for `holder`, bumping epoch by one and setting expires_at = now + ttl_s.

    Wins in one conditional UPDATE when the row is unclaimed, its expires_at is before `now`, or its
    holder already equals `holder`. Returns the new {holder, epoch, expires_at} on success, None when a
    live claim by someone else refuses it; the row is not modified on a refusal.
    """
    p = conn.dialect.placeholder
    until = _until(now, ttl_s)
    sql = (
        f"UPDATE work_items SET holder = {p}, epoch = epoch + 1, expires_at = {p} "
        f"WHERE initiative = {p} AND task_id = {p} AND (holder IS NULL OR expires_at < {p} OR holder = {p})"
    )
    with conn.transaction():
        won = conn.execute(sql, [holder, until, initiative, task, now, holder]) == 1
        if not won:
            return None
        row = conn.query_one(
            f"SELECT holder, epoch, expires_at FROM work_items WHERE initiative = {p} AND task_id = {p}",
            [initiative, task],
        )
    return None if row is None else {"holder": row[0], "epoch": row[1], "expires_at": row[2]}


def release(conn: Connection, initiative: str, task: str, holder: str) -> bool:
    """Clear holder and expires_at when the row's current holder equals `holder`; a mismatch changes nothing.

    Always safe to call: the return says whether a row was cleared, but the caller exits 0 either way.
    """
    p = conn.dialect.placeholder
    sql = f"UPDATE work_items SET holder = NULL, expires_at = NULL WHERE initiative = {p} AND task_id = {p} AND holder = {p}"
    return conn.execute(sql, [initiative, task, holder]) == 1


def _json_object(text: str) -> Row:
    try:
        value = json.loads(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not JSON: {text!r}") from None
    if not isinstance(value, dict):
        raise argparse.ArgumentTypeError("must be a JSON object")
    return value


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {text!r}") from None
    if value <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive number of seconds, got {value}")
    return value


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m harness.store_queue", description=__doc__.splitlines()[0])
    commands = ap.add_subparsers(dest="command", required=True)

    r = commands.add_parser("read", help="print one JSON object per queue row, one per line")
    r.add_argument("url")
    r.add_argument("--initiative")
    r.add_argument("--kind")

    u = commands.add_parser("upsert", help="insert or update one row keyed (initiative, task_id)")
    u.add_argument("url")
    u.add_argument("row_json", metavar="ROW_JSON", type=_json_object)

    c = commands.add_parser("claim", help="conditionally claim a row, bumping its epoch")
    c.add_argument("url")
    c.add_argument("initiative")
    c.add_argument("task")
    c.add_argument("holder")
    c.add_argument("ttl_s", metavar="TTL_S", type=_positive_int)
    c.add_argument("now", metavar="NOW")

    rel = commands.add_parser("release", help="clear a row's claim only when holder matches")
    rel.add_argument("url")
    rel.add_argument("initiative")
    rel.add_argument("task")
    rel.add_argument("holder")
    return ap


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return EXIT_BAD_INPUT


def dispatch(conn: Connection, args: argparse.Namespace) -> int:
    if args.command == "read":
        for row in read(conn, args.initiative, args.kind):
            print(json.dumps(row, sort_keys=True))
        return EXIT_OK
    if args.command == "upsert":
        upsert(conn, args.row_json)
        print(json.dumps(args.row_json, sort_keys=True))
        return EXIT_OK
    if args.command == "claim":
        claimed = claim(conn, args.initiative, args.task, args.holder, args.ttl_s, args.now)
        if claimed is None:
            print(f"error: {args.initiative}/{args.task} is held by another holder", file=sys.stderr)
            return EXIT_CLAIMED
        print(json.dumps(claimed, sort_keys=True))
        return EXIT_OK
    released = release(conn, args.initiative, args.task, args.holder)
    print(json.dumps({"released": released}, sort_keys=True))
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(sys.argv[1:] if argv is None else list(argv))
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_BAD_INPUT
    try:
        conn = connect(args.url)
    except (ValueError, OSError) as exc:
        return _fail(f"cannot open the store: {exc}")
    try:
        return dispatch(conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
