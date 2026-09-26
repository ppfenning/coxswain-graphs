"""The rule that mirrors work-file state into work_items rows. Pure: plain data in, plain data out."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

Row = dict[str, Any]


def item_row(initiative: str, item: Mapping[str, Any], updated_at: str | None, updated_by: str) -> Row:
    """task_id is the item id as given. The keys are assumed: store_write.work_item_row does not exist at this commit."""
    return {
        "initiative": initiative,
        "task_id": item["id"],
        "phase": item["phase"],
        "state": item["state"],
        "needs": list(item["needs"]),
        "updated_at": updated_at,
        "updated_by": updated_by,
    }


def _needs(value: Any) -> tuple[str, ...]:
    """needs is a set of ids: order and container type do not count as a difference."""
    return tuple(sorted(value or ()))


def _differs(file_item: Mapping[str, Any], store_row: Mapping[str, Any]) -> bool:
    return (
        file_item["state"] != store_row.get("state")
        or file_item["phase"] != store_row.get("phase")
        or _needs(file_item["needs"]) != _needs(store_row.get("needs"))
    )


def _disagreement(initiative: str, file_item: Mapping[str, Any], store_row: Mapping[str, Any]) -> Row:
    return {
        "initiative": initiative,
        "task_id": file_item["id"],
        "file_state": file_item["state"],
        "store_state": store_row.get("state"),
        "store_updated_at": store_row.get("updated_at"),
        "store_updated_by": store_row.get("updated_by"),
    }


def _decide(
    initiative: str,
    item: Mapping[str, Any],
    row: Mapping[str, Any] | None,
    file_time: str | None,
    fallback_time: str | None,
    by: str,
) -> tuple[Row | None, Row | None]:
    """(row to upsert, disagreement); at most one is set. An upsert keeps the later of the stored time and the file's."""
    if row is None:
        return (item_row(initiative, item, file_time or fallback_time, by), None)
    if file_time is None:
        # No file time means no disagreement can be shown, so the ticket's default applies: a differing row is
        # upserted, never silently left stale.
        row_time = row.get("updated_at") or ""
        if _differs(item, row):
            return (item_row(initiative, item, max(fallback_time or "", row_time) or None, by), None)
        return (None, None)
    row_time = row.get("updated_at") or ""
    if row_time > file_time and row.get("state") != item["state"]:
        return (None, _disagreement(initiative, item, row))
    if _differs(item, row):
        return (item_row(initiative, item, max(file_time, row_time), by), None)
    return (None, None)


def plan_mirror(
    initiative: str,
    file_items: Sequence[Mapping[str, Any]],
    store_rows: Sequence[Mapping[str, Any]],
    file_times: Mapping[str, str],
    by: str,
    *,
    fallback_time: str | None = None,
) -> tuple[list[Row], list[Row]]:
    """(rows to upsert, disagreements). store_rows are work_items rows as item_row builds them.

    Times are ISO strings compared as given; a stored row with no updated_at counts as older than any file time.
    A missing row is always upserted, stamped with the file time, else fallback_time, else None.
    A stored row whose item has no file time cannot be compared, so it is left alone and not reported.
    """
    by_task = {row["task_id"]: row for row in store_rows}
    decisions = [
        _decide(initiative, item, by_task.get(item["id"]), file_times.get(item["id"]), fallback_time, by)
        for item in file_items
    ]
    return [u for u, _ in decisions if u is not None], [d for _, d in decisions if d is not None]


USE_FILE = "use_file"
USE_FILE_AND_UPSERT = "use_file_and_upsert"
USE_STORE_REWRITE_FILE = "use_store_rewrite_file"
AGREE = "agree"


def authority_decision(work_state: str, file_state: str, row_state: str | None) -> str:
    """work_state is 'files' or 'store'. Under 'files' a differing row is upserted, as _decide does without times.

    USE_FILE is the files-mode result for a row newer than the file; that needs times this signature lacks.
    """
    if row_state is None:
        return USE_FILE_AND_UPSERT
    if row_state == file_state:
        return AGREE
    return USE_STORE_REWRITE_FILE if work_state == "store" else USE_FILE_AND_UPSERT


def _without_cr(line: str) -> str:
    return line[:-1] if line.endswith("\r") else line


def _state_token(value: str) -> tuple[str, str, str]:
    """(value, quote, tail) of a `state:` value: quotes and a ` #` comment are not part of the value."""
    if value[:1] in ("'", '"') and value[0] in value[1:]:
        end = value.index(value[0], 1) + 1
        return (value[1 : end - 1], value[0], value[end:])
    comment = re.search(r"\s#", value)
    token = (value if comment is None else value[: comment.start()]).rstrip()
    return (token, "", value[len(token) :])


def set_frontmatter_state(text: str, new_state: str) -> str | None:
    """text with only the first frontmatter `state:` value replaced; None if there is no such line. Splits on \\n only."""
    lines = text.split("\n")
    close = next((i for i, line in enumerate(lines[1:], 1) if _without_cr(line) == "---"), None)
    if _without_cr(lines[0]) != "---" or close is None:
        return None
    at = next((i for i in range(1, close) if lines[i].startswith("state:")), None)
    if at is None:
        return None
    line = lines[at]
    cr = line[len(_without_cr(line)) :]
    rest = _without_cr(line)[len("state:") :]
    body = rest.lstrip()
    value, quote, tail = _state_token(body)
    if value == new_state:
        return text
    lead = rest[: len(rest) - len(body)] or " "
    new_line = "state:" + lead + quote + new_state + quote + tail + cr
    return "\n".join([*lines[:at], new_line, *lines[at + 1 :]])


def check_expected_state(expected: str, actual: str | None) -> tuple[bool, str]:
    """(allow, reason). The reason names both states; a missing row reads as 'no row'."""
    if expected == actual:
        return (True, "row state is " + repr(expected) + " as expected")
    shown = "no row" if actual is None else repr(actual)
    return (False, "expected state " + repr(expected) + " but store has " + shown)
