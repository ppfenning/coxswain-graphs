"""Give resumed calls their own cost.

A resumed Claude Code session reports its running total on every call, so the stored cost_usd of a
later call in a session includes every call before it. `recost` turns those totals into per-call
spend. The session id is not stored; it is read from the `system` `init` event of each call's trace.
A call with no readable trace has no session id and is left alone.

The reported figure moves to detail_json as `reported_cost_usd`. A call carrying it is never changed
again, but its reported figure still counts as the previous call's total for a later call.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, NamedTuple

from harness import store_traces
from harness.store_dialect import Connection, json_load, json_text
from harness.store_write import Store
from harness.traces_url import TracesRoot

REPORTED_KEY = "reported_cost_usd"
DECIMALS = 6

Call = tuple[str, str | None, str, float]


class Change(NamedTuple):
    call_id: str
    cost: float
    reported: float
    detail: dict[str, Any]


def _own_cost(previous: float | None, reported: float) -> float:
    """A first call or a drop in the running total (a restarted session) keeps its reported cost."""
    return reported if previous is None or reported < previous else round(reported - previous, DECIMALS)


def recost(calls: Sequence[Call]) -> list[tuple[str, float, float]]:
    """(call_id, own cost, reported cost) per call with a session id, ordered by session then ts.

    `calls` is (call_id, session_id, ts, reported_cost). The difference is rounded to 6 decimals.
    """
    known = sorted((c for c in calls if c[1]), key=lambda c: (c[1], c[2], c[0]))
    return [
        (call_id, _own_cost(before[3] if before is not None and before[1] == session else None, reported), reported)
        for (call_id, session, _ts, reported), before in zip(known, [None, *known], strict=False)
    ]


def session_id_of(events: Sequence[dict[str, Any]]) -> str | None:
    """The session_id of the first system init event, else None."""
    inits = [e for e in events if e.get("type") == "system" and e.get("subtype") == "init"]
    session = inits[0].get("session_id") if inits else None
    return session if isinstance(session, str) and session else None


def read_session_id(traces: TracesRoot | str, run_id: str, call_id: str) -> str | None:
    """Edge: the call's session id, or None when its trace is missing or cannot be read."""
    try:
        return session_id_of(store_traces.read_call(traces, run_id, call_id))
    except (OSError, ValueError, KeyError, store_traces.TracesUnavailable, store_traces.ParquetUnavailable):
        return None


def plan_recost(conn: Connection, traces: TracesRoot | str) -> list[Change]:
    """Calls whose own cost differs from the stored one, and that do not carry `reported_cost_usd` yet. Reads only."""
    rows = conn.query_all(
        "SELECT call_id, run_id, ts, cost_usd, detail_json FROM node_calls WHERE cost_usd IS NOT NULL ORDER BY run_id, seq"
    )
    details = {call_id: d if isinstance(d := json_load(raw), dict) else {} for call_id, _r, _t, _c, raw in rows}
    calls = [
        (call_id, read_session_id(traces, run_id, call_id), ts or "", float(details[call_id].get(REPORTED_KEY, cost)))
        for call_id, run_id, ts, cost, _raw in rows
    ]
    return [
        Change(call_id, cost, reported, details[call_id])
        for call_id, cost, reported in recost(calls)
        if REPORTED_KEY not in details[call_id] and cost != reported
    ]


def apply_recost(store: Store, changes: Sequence[Change]) -> int:
    """Set each call's cost_usd to its own cost and merge the old figure into detail_json. Rows changed."""
    mark = store.conn.dialect.placeholder
    update = f"UPDATE node_calls SET cost_usd = {mark}, detail_json = {mark} WHERE call_id = {mark}"
    with store.conn.transaction():
        return sum(
            store.conn.execute(update, (c.cost, json_text({**c.detail, REPORTED_KEY: c.reported}), c.call_id))
            for c in changes
        )


def totals(changes: Sequence[Change]) -> tuple[int, float, float]:
    """(calls, sum of cost before, sum of cost after)."""
    return len(changes), sum(c.reported for c in changes), sum(c.cost for c in changes)
