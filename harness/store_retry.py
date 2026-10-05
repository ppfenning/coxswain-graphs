"""Retry a store write across a dropped connection: back off, reconnect, try again until a deadline."""

from collections.abc import Callable
from itertools import count
from typing import TypeVar

T = TypeVar("T")

# Lower-cased fragments of the libpq and psycopg messages for a connection that dropped or cannot be made yet.
# An OperationalError without one, such as a failed password or a missing database, is a fault, not an outage.
_CONNECTION_LOST = (
    "the connection is lost",
    "server closed the connection",
    "terminating connection",
    "connection refused",
    "could not connect",
    "connection timed out",
    "timeout expired",
    "connection reset",
    "no route to host",
    "the database system is starting up",
    "the database system is shutting down",
    "consuming input failed",
    "sending query failed",
)


def _is_psycopg_operational(exc: BaseException) -> bool:
    # Matched by name, not by `import psycopg`: the driver is an optional extra, and an import guard
    # would leave this module retrying nothing, silently, wherever the extra is not installed.
    return any(
        cls.__name__ == "OperationalError" and cls.__module__.split(".")[0] == "psycopg" for cls in type(exc).__mro__
    )


def is_connection_error(exc: BaseException) -> bool:
    """A psycopg OperationalError whose message says the connection dropped or was refused."""
    message = str(exc).lower()
    return _is_psycopg_operational(exc) and any(fragment in message for fragment in _CONNECTION_LOST)


def retry_store_write(
    write: Callable[[], T],
    *,
    reconnect: Callable[[], None],
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    window_s: float = 300.0,
    base_s: float = 1.0,
) -> T:
    """Sleeps base_s * 2**n before each retry, cut so the last attempt starts at clock() + window_s, never after."""
    deadline = clock() + window_s
    for attempt in count():
        try:
            if attempt:
                reconnect()
            return write()
        except Exception as exc:
            remaining = deadline - clock()
            if not is_connection_error(exc) or remaining <= 0:
                raise
            sleep(min(base_s * 2**attempt, remaining))
    raise AssertionError("unreachable: count() does not end")
