"""Lease command handlers: wrap store_lease into `{ok, epoch, holder}` dicts.

A refusal is a value. `store_lease.acquire` reports the current epoch and holder;
`renew` and `release` return a bare bool, so their dicts carry epoch and holder None.
Nothing here reads the clock: `now` is always an argument.
"""

from __future__ import annotations

from typing import Any

from harness import store_lease
from harness.store_dialect import Connection

__all__ = ["lease_acquire", "lease_release", "lease_renew"]


def _result(ok: bool, epoch: int | None, holder: str | None) -> dict[str, Any]:
    return {"ok": ok, "epoch": epoch, "holder": holder}


def lease_acquire(conn: Connection, name: str, holder: str, now: str, ttl: int) -> dict[str, Any]:
    taken = store_lease.acquire(conn, name, holder, now, ttl)
    return _result(taken.ok, taken.epoch, taken.holder)


def lease_renew(conn: Connection, name: str, holder: str, epoch: int, now: str, ttl: int) -> dict[str, Any]:
    return _result(store_lease.renew(conn, name, holder, epoch, now, ttl), None, None)


def lease_release(conn: Connection, name: str, holder: str, epoch: int) -> dict[str, Any]:
    return _result(store_lease.release(conn, name, holder, epoch), None, None)
