"""A run's lease: its name, and the thread that keeps it alive.

Time is an argument here too: the heartbeat is handed a clock and never reads one.
"""

from __future__ import annotations

import re
import sys
import threading
from collections.abc import Callable

from harness.store_lease import renew
from harness.store_migrate import open_store

__all__ = ["Heartbeat", "lease_name"]

_NUMERIC_SUFFIX = re.compile(r"-\d+$")


def lease_name(run_id: str) -> str:
    """`runs:<prefix>`: the run id without a trailing `-<digits>`, so `x-2` and `x-3` contend for one lease."""
    return f"runs:{_NUMERIC_SUFFIX.sub('', run_id)}"


class Heartbeat:
    """Renews a lease every `interval` seconds on its own connection until stopped or the renew fails.

    A failed renew warns once on stderr and ends the thread. It never touches the run:
    the epoch fence is what stops a stale writer. A store error is not retried here: a
    SQLite connection already waits 30 s on a busy lock (`store_dialect.connect`), so an
    error that reaches this loop is not the transient contention a retry would cure.
    """

    def __init__(
        self,
        url: str,
        name: str,
        holder: str,
        epoch: int,
        *,
        clock: Callable[[], str],
        interval: float = 30.0,
        ttl: int = 120,
    ) -> None:
        self._url, self._name, self._holder, self._epoch = url, name, holder, epoch
        self._clock, self._interval, self._ttl = clock, interval, ttl
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name=f"heartbeat-{name}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        """Signal the thread and wait at most `timeout` seconds; a thread stuck in a store call is left as a daemon."""
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout)
        if self._thread.is_alive():
            self._warn(f"still in a store call after {timeout:g}s; left as a daemon")

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    def _loop(self) -> None:
        # SQLite connections are not shared across threads, so this thread opens its own.
        try:
            conn = open_store(self._url, self._clock())
        except Exception as exc:
            self._warn(f"cannot open the store: {' '.join(str(exc).split())}")
            return
        try:
            while not self._stop.wait(self._interval):
                if not renew(conn, self._name, self._holder, self._epoch, self._clock(), self._ttl):
                    self._warn(f"stale epoch {self._epoch}")
                    return
        except Exception as exc:
            self._warn(" ".join(str(exc).split()))
        finally:
            conn.close()

    def _warn(self, reason: str) -> None:
        print(f"lease: heartbeat for {self._name} stopped: {reason}", file=sys.stderr)
