"""Leader lease: one row in the `leases` table, fenced by a monotonic epoch.

Time is always an argument: an ISO timestamp string and a ttl in seconds. Nothing
here reads the clock. Every state change is one conditional statement whose
affected row count decides the outcome, so two writers cannot both win.
Timestamps are stored as `YYYY-MM-DDTHH:MM:SSZ`, which sorts as text.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from harness.store_dialect import Connection, Dialect, insert_ignore

__all__ = ["LeaseResult", "acquire", "assert_epoch", "lease_state", "release", "renew"]

_PAST = "1970-01-01T00:00:00Z"
_COLUMNS = ("name", "holder", "epoch", "heartbeat_at", "expires_at")

# `?` is the placeholder token; `_sql` swaps in the engine's own.
_TAKE = (
    "UPDATE leases SET holder = ?, epoch = epoch + 1, heartbeat_at = ?, expires_at = ? "
    "WHERE name = ? AND (expires_at <= ? OR holder = ?)"
)
_RENEW = (
    "UPDATE leases SET heartbeat_at = ?, expires_at = ? WHERE name = ? AND holder = ? AND epoch = ? AND expires_at > ?"
)
_RELEASE = "UPDATE leases SET expires_at = ? WHERE name = ? AND holder = ? AND epoch = ?"
_READ = "SELECT name, holder, epoch, heartbeat_at, expires_at FROM leases WHERE name = ?"
_FENCE = "SELECT 1 FROM leases WHERE name = ? AND epoch = ? AND expires_at > ?"

SQL = (_TAKE, _RENEW, _RELEASE, _READ, _FENCE)


@dataclass(frozen=True)
class LeaseResult:
    ok: bool
    epoch: int | None
    holder: str | None


def _sql(dialect: Dialect, text: str) -> str:
    return text.replace("?", dialect.placeholder)


def _stamp(iso: str) -> str:
    """Normalise an ISO timestamp to UTC `YYYY-MM-DDTHH:MM:SSZ`."""
    parsed = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    aware = parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return aware.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _expiry(now: str, ttl: int) -> str:
    parsed = datetime.fromisoformat(_stamp(now).replace("Z", "+00:00"))
    return (parsed + timedelta(seconds=ttl)).strftime("%Y-%m-%dT%H:%M:%SZ")


def lease_state(row: tuple[Any, ...] | None, now: str) -> str:
    """`free` for no row, `expired` once expires_at is not after now, else `held`. Row is the leases column order."""
    if row is None:
        return "free"
    if _stamp(row[4]) <= _stamp(now):
        return "expired"
    return "held"


def _read(conn: Connection, name: str) -> tuple[Any, ...] | None:
    return conn.query_one(_sql(conn.dialect, _READ), (name,))


def acquire(conn: Connection, name: str, holder: str, now: str, ttl: int) -> LeaseResult:
    """Take a free or expired lease, or renew-with-increment one already held by `holder`.

    A refusal, including a lost insert race, is a result with ok False and the current holder.
    """
    at, until = _stamp(now), _expiry(now, ttl)
    with conn.transaction():
        if conn.execute(_sql(conn.dialect, _TAKE), (holder, at, until, name, at, holder)) == 1:
            row = _read(conn, name)
            return LeaseResult(True, None if row is None else int(row[2]), holder)
        insert = insert_ignore(conn.dialect, "leases", _COLUMNS, ("name",))
        if conn.execute(insert, (name, holder, 1, at, until)) == 1:
            return LeaseResult(True, 1, holder)
        row = _read(conn, name)
        return LeaseResult(False, None if row is None else int(row[2]), None if row is None else row[1])


def renew(conn: Connection, name: str, holder: str, epoch: int, now: str, ttl: int) -> bool:
    """Extend a live lease. An expired or released lease is not revived: the holder must acquire again."""
    at = _stamp(now)
    return conn.execute(_sql(conn.dialect, _RENEW), (at, _expiry(now, ttl), name, holder, epoch, at)) == 1


def release(conn: Connection, name: str, holder: str, epoch: int) -> bool:
    """Expire the lease in the past. The row and its epoch stay, so the next holder still increments."""
    return conn.execute(_sql(conn.dialect, _RELEASE), (_PAST, name, holder, epoch)) == 1


def assert_epoch(conn: Connection, name: str, epoch: int, now: str) -> bool:
    """The fence: true only when `epoch` is the stored one and the lease is unexpired at `now`.

    Every leader write calls this immediately before it writes.
    """
    return conn.query_one(_sql(conn.dialect, _FENCE), (name, epoch, _stamp(now))) is not None
