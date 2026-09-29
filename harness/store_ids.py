"""Short initiative ids: allocate one, backfill every initiative that lacks one, and resolve
a token back to the row it names.

Against the schema landed in migration 0011: `id_sequence` holds one row per scope, `work_items`
and `runs` each gain a nullable `short_id`. The three functions below keep their logic in plain
data; only the transaction and the row lock that guards it are I/O. This module lands
inert: nothing calls `allocate_initiative` yet, and `python -m harness.store_ids` is its own
entry point, matching the flag and subcommand shape of `harness.store_cli`.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, NamedTuple

from harness.cli import _read_profile
from harness.store_cli import _DB_ERRORS, _OPEN_ERRORS, _common, resolve_store_url
from harness.store_dialect import Connection, insert_ignore
from harness.store_migrate import open_store

EXIT_OK = 0
EXIT_BAD_INPUT = 2
EXIT_NOT_FOUND = 3

INTAKE_TASK = "intake"
INTAKE_INITIATIVE = "intake"

# A run's own trailing number, split from the initiative stem it belongs to: "x-7" -> ("x", "7").
_RUN_SPLIT = re.compile(r"^(?P<stem>.+)-(?P<n>\d+)$")
# A short id: "I3" (an intake row), "I3-t2" (a work item), "I3-7" (a run).
_SHORT_ID = re.compile(r"^I\d+(?:-t?\d+)?$")
# The initiative number every short id starts with, and the task number a work item's carries.
_SHORT_N = re.compile(r"^I(?P<n>\d+)")
_TASK_K = re.compile(r"^I\d+-t(?P<k>\d+)$")


class ItemRow(NamedTuple):
    initiative: str
    task_id: str
    phase: str
    updated_at: str
    short_id: str | None


class RunRow(NamedTuple):
    run_id: str
    started_at: str
    short_id: str | None


class Update(NamedTuple):
    table: str
    key: tuple[str, ...]
    short_id: str


class BackfillPlan(NamedTuple):
    numbered: tuple[str, ...]
    item_updates: tuple[Update, ...]
    run_updates: tuple[Update, ...]
    next_n: int


def _run_stem(run_id: str) -> str:
    """The initiative name a run belongs to: `run_id` with its trailing `-<digits>` removed."""
    m = _RUN_SPLIT.match(run_id)
    return m.group("stem") if m else run_id


def _run_suffix(run_id: str) -> str:
    """The run's own trailing number, as text; `run_id` itself when it carries none."""
    m = _RUN_SPLIT.match(run_id)
    return m.group("n") if m else run_id


def _initiative_names(items: Sequence[ItemRow], runs: Sequence[RunRow]) -> frozenset[str]:
    """Every initiative visible in `items` or `runs`, the literal 'intake' bucket excluded."""
    from_items = {r.initiative for r in items if r.initiative != INTAKE_INITIATIVE}
    from_runs = {_run_stem(r.run_id) for r in runs}
    return frozenset(from_items | from_runs)


def _existing_number(name: str, items: Sequence[ItemRow], runs: Sequence[RunRow]) -> int | None:
    """The n an earlier backfill or allocation gave `name`, read off any of its short ids; None if it has none."""
    ids = [
        *(r.short_id for r in items if r.initiative == name and r.short_id is not None),
        *(r.short_id for r in runs if _run_stem(r.run_id) == name and r.short_id is not None),
    ]
    found = [int(m.group("n")) for m in map(_SHORT_N.match, ids) if m]
    return min(found) if found else None


def _max_task_k(name: str, items: Sequence[ItemRow]) -> int:
    """The highest k among `name`'s numbered work items; 0 when none carries one."""
    ks = [
        int(m.group("k"))
        for m in (_TASK_K.match(r.short_id) for r in items if r.initiative == name and r.short_id is not None)
        if m
    ]
    return max(ks, default=0)


def _initiative_date(name: str, items: Sequence[ItemRow], runs: Sequence[RunRow]) -> str:
    """The earlier of `name`'s intake-row date and its earliest run start; '' when neither exists."""
    intake_date = next((r.updated_at for r in items if r.initiative == name and r.task_id == INTAKE_TASK), None)
    starts = sorted(r.started_at for r in runs if _run_stem(r.run_id) == name)
    candidates = [d for d in (intake_date, starts[0] if starts else None) if d is not None]
    return min(candidates) if candidates else ""


def _new_initiatives(items: Sequence[ItemRow], runs: Sequence[RunRow]) -> tuple[str, ...]:
    """Un-numbered initiative names, ordered by first appearance, ties broken by name."""
    names = [n for n in _initiative_names(items, runs) if _existing_number(n, items, runs) is None]
    return tuple(sorted(names, key=lambda n: (_initiative_date(n, items, runs), n)))


def _item_updates(name: str, n: int, items: Sequence[ItemRow]) -> tuple[Update, ...]:
    """Short ids for `name`'s un-numbered work items; new tasks continue after its highest t{k}."""
    intake = tuple(
        Update("work_items", (name, INTAKE_TASK), f"I{n}")
        for r in items
        if r.initiative == name and r.task_id == INTAKE_TASK and r.short_id is None
    )
    tasks = sorted(
        (r for r in items if r.initiative == name and r.task_id != INTAKE_TASK and r.short_id is None),
        key=lambda r: (r.phase, r.task_id),
    )
    return intake + tuple(
        Update("work_items", (name, r.task_id), f"I{n}-t{k}")
        for k, r in enumerate(tasks, start=_max_task_k(name, items) + 1)
    )


def _run_updates(name: str, n: int, runs: Sequence[RunRow]) -> tuple[Update, ...]:
    """Short ids for `name`'s un-numbered runs, each keeping its own trailing number."""
    return tuple(
        Update("runs", (r.run_id,), f"I{n}-{_run_suffix(r.run_id)}")
        for r in runs
        if _run_stem(r.run_id) == name and r.short_id is None
    )


def _plan(items: Sequence[ItemRow], runs: Sequence[RunRow], start_n: int) -> BackfillPlan:
    """Pure core of `backfill`: which rows get which short id, and where id_sequence lands next.

    A new initiative takes the next number from `start_n`; an already-numbered one keeps its n,
    and only its rows without a short id are topped up, without moving id_sequence."""
    ordered = _new_initiatives(items, runs)
    existing = {
        name: n for name in _initiative_names(items, runs) if (n := _existing_number(name, items, runs)) is not None
    }
    numbers = sorted({**existing, **{name: start_n + i for i, name in enumerate(ordered)}}.items(), key=lambda kv: kv[1])
    return BackfillPlan(
        ordered,
        tuple(u for name, n in numbers for u in _item_updates(name, n, items)),
        tuple(u for name, n in numbers for u in _run_updates(name, n, runs)),
        start_n + len(ordered),
    )


def _read_next(tx: Connection, scope: str) -> int:
    row = tx.query_one(f"SELECT next FROM id_sequence WHERE scope = {tx.dialect.placeholder}", (scope,))
    return 1 if row is None else int(row[0])


def _write_next(tx: Connection, scope: str, value: int) -> None:
    p = tx.dialect.placeholder
    if tx.execute(f"UPDATE id_sequence SET next = {p} WHERE scope = {p}", (value, scope)) == 0:
        tx.execute(f"INSERT INTO id_sequence (scope, next) VALUES ({p}, {p})", (scope, value))


def _lock_row(tx: Connection, scope: str) -> None:
    """Postgres: create the scope's row at 1 if absent, then hold its row lock until commit.

    The insert-ignore gives FOR UPDATE a row to lock even on first use; a concurrent first
    insert waits on the conflict. sqlite's own `BEGIN IMMEDIATE` already holds the write lock."""
    if tx.dialect.name == "postgres":
        p = tx.dialect.placeholder
        tx.execute(insert_ignore(tx.dialect, "id_sequence", ("scope", "next"), ("scope",)), (scope, 1))
        tx.query_one(f"SELECT next FROM id_sequence WHERE scope = {p} FOR UPDATE", (scope,))


def allocate_initiative(conn: Connection) -> str:
    """Read-and-advance the id_sequence row keyed 'initiative', creating it at 1 if absent."""
    with conn.transaction() as tx:
        _lock_row(tx, "initiative")
        n = _read_next(tx, "initiative")
        _write_next(tx, "initiative", n + 1)
    return f"I{n}"


def backfill(conn: Connection) -> dict[str, int]:
    """Set a short id on every row without one, numbering new initiatives. Idempotent."""
    with conn.transaction() as tx:
        _lock_row(tx, "initiative")
        items = tuple(
            ItemRow(*row)
            for row in tx.query_all("SELECT initiative, task_id, phase, updated_at, short_id FROM work_items")
        )
        runs = tuple(RunRow(*row) for row in tx.query_all("SELECT run_id, started_at, short_id FROM runs"))
        plan = _plan(items, runs, _read_next(tx, "initiative"))
        for update in plan.item_updates:
            tx.execute(
                f"UPDATE work_items SET short_id = {tx.dialect.placeholder} "
                f"WHERE initiative = {tx.dialect.placeholder} AND task_id = {tx.dialect.placeholder}",
                (update.short_id, *update.key),
            )
        for update in plan.run_updates:
            tx.execute(
                f"UPDATE runs SET short_id = {tx.dialect.placeholder} WHERE run_id = {tx.dialect.placeholder}",
                (update.short_id, *update.key),
            )
        if plan.numbered:
            _write_next(tx, "initiative", plan.next_n)
    return {
        "initiatives": len(plan.numbered),
        "work_items": len(plan.item_updates),
        "runs": len(plan.run_updates),
    }


def resolve(conn: Connection, token: str) -> tuple[str, str] | None:
    """(kind, old-style key) for the row `token` names, by short id or by its old key; None if no row matches."""
    p = conn.dialect.placeholder
    if _SHORT_ID.match(token):
        row = conn.query_one(f"SELECT initiative, task_id FROM work_items WHERE short_id = {p}", (token,))
        if row is not None:
            return ("work_item", f"{row[0]}/{row[1]}")
        row = conn.query_one(f"SELECT run_id FROM runs WHERE short_id = {p}", (token,))
        return ("run", row[0]) if row is not None else None
    if "/" in token:
        initiative, _, task_id = token.partition("/")
        row = conn.query_one(
            f"SELECT initiative, task_id FROM work_items WHERE initiative = {p} AND task_id = {p}",
            (initiative, task_id),
        )
        return ("work_item", token) if row is not None else None
    row = conn.query_one(f"SELECT run_id FROM runs WHERE run_id = {p}", (token,))
    return ("run", row[0]) if row is not None else None


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m harness.store_ids", description=__doc__.splitlines()[0])
    _common(ap, top=True)
    commands = ap.add_subparsers(dest="command", required=True)

    backfill_p = commands.add_parser("backfill", help="number every un-numbered initiative and set its short ids")
    _common(backfill_p, top=False)
    backfill_p.add_argument("--json", action="store_true", help="print the result as one JSON object")

    allocate_p = commands.add_parser("allocate", help="allocate the next initiative short id")
    _common(allocate_p, top=False)
    allocate_p.add_argument("--json", action="store_true", help="print the result as one JSON object")

    resolve_p = commands.add_parser("resolve", help="the (kind, key) a short id or an old-style key names")
    _common(resolve_p, top=False)
    resolve_p.add_argument("token")
    resolve_p.add_argument("--json", action="store_true", help="print the result as one JSON object")

    return ap


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return EXIT_BAD_INPUT


def _dispatch(conn: Connection, args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    if args.command == "allocate":
        return {"short_id": allocate_initiative(conn)}, EXIT_OK
    if args.command == "backfill":
        return backfill(conn), EXIT_OK
    found = resolve(conn, args.token)
    if found is None:
        return {"token": args.token, "kind": None, "key": None}, EXIT_NOT_FOUND
    kind, key = found
    return {"token": args.token, "kind": kind, "key": key}, EXIT_OK


def _summary(command: str, payload: Mapping[str, Any]) -> str:
    if command == "allocate":
        return str(payload["short_id"])
    if command == "backfill":
        return " ".join(f"{k}={payload[k]}" for k in ("initiatives", "work_items", "runs"))
    if payload["kind"] is None:
        return f"no match for {payload['token']!r}"
    return f"{payload['kind']} {payload['key']}"


def main(argv: Sequence[str] | None = None) -> int:
    try:
        with contextlib.redirect_stdout(sys.stderr):
            args = build_parser().parse_args(sys.argv[1:] if argv is None else list(argv))
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_BAD_INPUT
    if not args.store_url and args.runs_dir is None:
        return _fail("--store-url or --runs-dir is required")
    profile = {} if args.provider_profile is None else _read_profile(args.provider_profile)
    url = resolve_store_url(args.store_url, profile, args.runs_dir)
    try:
        conn = open_store(url, _now())
    except _OPEN_ERRORS as exc:
        return _fail(f"cannot open the store: {exc}")
    try:
        payload, code = _dispatch(conn, args)
    except _DB_ERRORS as exc:
        return _fail(f"cannot read the store: {exc}")
    finally:
        conn.close()
    if code == EXIT_NOT_FOUND:
        print(f"error: no match for {args.token!r}", file=sys.stderr)
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(_summary(args.command, payload))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
