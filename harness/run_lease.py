"""A run's lease: its name, and the thread that keeps it alive.

Time is an argument here too: the heartbeat is handed a clock and never reads one.
"""

from __future__ import annotations

import contextlib
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


def _close(conn) -> None:
    with contextlib.suppress(Exception):
        conn.close()


class Heartbeat:
    """Renews a lease every `interval` seconds on its own connection until stopped or the renew fails.

    A stale epoch warns once on stderr and ends the thread at once. It never touches the run:
    the epoch fence is what stops a stale writer. A renew or open that raises is retried on a
    fresh connection, `attempts` tries in all with `retry_wait` seconds between them, and
    only the last failure warns and ends the thread.
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
        attempts: int = 3,
        retry_wait: float = 2.0,
    ) -> None:
        self._url, self._name, self._holder, self._epoch = url, name, holder, epoch
        self._clock, self._interval, self._ttl = clock, interval, ttl
        self._attempts, self._retry_wait = max(1, attempts), retry_wait
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
        conn = self._open()
        while conn is not None and not self._stop.wait(self._interval):
            conn = self._tick(conn)
        if conn is not None:
            _close(conn)

    def _open(self):
        """A connection, or None after `attempts` failed opens or a stop during a retry wait."""
        error = ""
        for attempt in range(self._attempts):
            if attempt and self._stop.wait(self._retry_wait):
                return None
            try:
                return open_store(self._url, self._clock())
            except Exception as exc:
                error = " ".join(str(exc).split())
        self._warn(f"cannot open the store: {error} after {self._attempts} attempts")
        return None

    def _tick(self, conn):
        """Renew once, retrying an error on a fresh connection. Returns the live connection, or None when the thread ends."""
        error = ""
        for attempt in range(self._attempts):
            if attempt:
                if self._stop.wait(self._retry_wait):
                    return None
                try:
                    conn = open_store(self._url, self._clock())
                except Exception as exc:
                    error = " ".join(str(exc).split())
                    continue
            try:
                if renew(conn, self._name, self._holder, self._epoch, self._clock(), self._ttl):
                    return conn
                self._warn(f"stale epoch {self._epoch}")
                _close(conn)
                return None
            except Exception as exc:
                error = " ".join(str(exc).split())
                _close(conn)
        self._warn(f"{error} after {self._attempts} attempts")
        return None

    def _warn(self, reason: str) -> None:
        print(f"lease: heartbeat for {self._name} stopped: {reason}", file=sys.stderr)
