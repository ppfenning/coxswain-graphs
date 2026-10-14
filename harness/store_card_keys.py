"""python -m harness.store_card_keys URL: copy card keys from extra_json into their columns.

Migration 0013 added issue_number, project_item_id and parent_item to work_items and left
them NULL. Older rows keep the issue number inside extra_json under "issue". This copies
what extra can state outright and guesses nothing. It is run by a person against a store;
no migration or startup path calls it.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping, Sequence
from typing import Any

from harness.store_dialect import Connection, connect, json_load

__all__ = ["backfill", "card_keys"]

EXIT_OK = 0
EXIT_BAD_INPUT = 2

_COLUMNS = ("issue_number", "project_item_id", "parent_item")


def card_keys(extra: Mapping[str, Any] | None) -> dict[str, Any]:
    """The card columns extra can fill; a value that is not clearly one is left out."""
    if not isinstance(extra, Mapping):
        return {}
    issue = extra.get("issue")
    if isinstance(issue, int) and not isinstance(issue, bool):
        found: dict[str, Any] = {"issue_number": issue}
    elif isinstance(issue, str) and issue.isascii() and issue.isdigit():
        found = {"issue_number": int(issue)}
    else:
        found = {}
    texts = {
        name: extra[name]
        for name in ("project_item_id", "parent_item")
        if isinstance(extra.get(name), str) and extra[name]
    }
    return {**found, **texts}


def backfill(conn: Connection) -> int:
    """Fill NULL card columns from extra_json in one transaction; return the rows changed."""
    ph = conn.dialect.placeholder
    changed = 0
    with conn.transaction():
        rows = conn.query_all(
            "SELECT initiative, task_id, extra_json, issue_number, project_item_id, parent_item"
            " FROM work_items WHERE extra_json IS NOT NULL"
        )
        for initiative, task_id, extra_json, *current in rows:
            held = dict(zip(_COLUMNS, current, strict=True))
            fills = {name: value for name, value in card_keys(json_load(extra_json)).items() if held[name] is None}
            if not fills:
                continue
            sets = ", ".join(f"{name} = {ph}" for name in fills)
            conn.execute(
                f"UPDATE work_items SET {sets} WHERE initiative = {ph} AND task_id = {ph}",
                (*fills.values(), initiative, task_id),
            )
            changed += 1
    return changed


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m harness.store_card_keys",
        description="Copy issue, project_item_id and parent_item from extra_json into NULL card columns.",
    )
    ap.add_argument("url")
    return ap


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(sys.argv[1:] if argv is None else list(argv))
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_BAD_INPUT
    try:
        conn = connect(args.url)
    except (ValueError, OSError) as exc:
        print(f"error: cannot open the store: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT
    try:
        print(backfill(conn))
    finally:
        conn.close()
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
