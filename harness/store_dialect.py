"""The one module that knows which database engine is in use.

Other store modules take a `Connection` from `connect` and ask its dialect for
engine-specific text. Importing this needs only the standard library: the
Postgres driver is imported inside `connect`, on first use.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

_T = TypeVar("_T")

__all__ = [
    "POSTGRES",
    "SQLITE",
    "Connection",
    "Dialect",
    "StoreDriverMissing",
    "connect",
    "default_url",
    "forbidden_constructs",
    "insert_ignore",
    "json_load",
    "json_text",
    "upsert",
]


class StoreDriverMissing(RuntimeError):
    """The Postgres driver is not installed; the `postgres` extra provides it."""


@dataclass(frozen=True)
class Dialect:
    name: str
    placeholder: str
    json_type: str
    bool_type: str  # booleans are stored as 0/1 in this column type on every engine


SQLITE = Dialect(name="sqlite", placeholder="?", json_type="TEXT", bool_type="SMALLINT")
POSTGRES = Dialect(name="postgres", placeholder="%s", json_type="JSONB", bool_type="SMALLINT")

# Token to report, pattern that finds it. Order is the report order.
# REPLACE INTO is sqlite's alias for INSERT OR REPLACE, so it reports under that token.
# strftime and json_extract count only as calls, so a column of that name passes.
_FORBIDDEN: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (token, re.compile(pattern, re.IGNORECASE))
    for token, pattern in (
        ("AUTOINCREMENT", r"\bAUTOINCREMENT\b"),
        ("PRAGMA", r"\bPRAGMA\b"),
        ("INSERT OR REPLACE", r"\bINSERT\s+OR\s+REPLACE\b|\bREPLACE\s+INTO\b"),
        ("strftime", r"\bstrftime\s*\("),
        ("json_extract", r"\bjson_extract\s*\("),
    )
)

# Comments and quoted text: blanked before matching so they neither hide nor fake a token.
_NOT_CODE = re.compile(r"--[^\n]*|/\*.*?\*/|'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"", re.DOTALL)


def forbidden_constructs(sql: str) -> tuple[str, ...]:
    """Banned non-portable tokens found in `sql`, deduplicated, in a fixed order."""
    code = _NOT_CODE.sub(" ", sql)
    return tuple(token for token, rx in _FORBIDDEN if rx.search(code))


def _insert(dialect: Dialect, table: str, columns: Sequence[str]) -> str:
    marks = ", ".join(dialect.placeholder for _ in columns)
    return f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({marks})"


def insert_ignore(dialect: Dialect, table: str, columns: Sequence[str], key_columns: Sequence[str]) -> str:
    """Insert that skips a row whose key already exists."""
    return f"{_insert(dialect, table, columns)} ON CONFLICT ({', '.join(key_columns)}) DO NOTHING"


def upsert(dialect: Dialect, table: str, columns: Sequence[str], key_columns: Sequence[str]) -> str:
    """Insert that overwrites every non-key column on key conflict; DO NOTHING if all are keys."""
    if not key_columns:
        raise ValueError("upsert needs at least one key column")
    keys = ", ".join(key_columns)
    sets = ", ".join(f"{c} = excluded.{c}" for c in columns if c not in key_columns)
    action = f"DO UPDATE SET {sets}" if sets else "DO NOTHING"
    return f"{_insert(dialect, table, columns)} ON CONFLICT ({keys}) {action}"


def json_text(value: Any) -> str:
    """Serialise a value for a json column; keys sorted so equal values give equal text."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def json_load(raw: str | bytes | None) -> Any:
    """Inverse of `json_text`; None stays None. `connect` makes both engines return json as text."""
    return None if raw is None else json.loads(raw)


class Connection:
    """Thin wrapper over a DB-API connection that `connect` opened in driver autocommit mode.

    Outside `transaction` each statement stands alone, and a failed one leaves no
    transaction open. `transaction` owns BEGIN, COMMIT and ROLLBACK as plain SQL.
    One lock serialises the threads sharing it (a runner's calls run on a thread pool):
    a statement waits while another thread's transaction is open, and never joins it.
    """

    def __init__(self, raw: Any, dialect: Dialect, begin: str = "BEGIN") -> None:
        self.raw = raw
        self.dialect = dialect
        self._begin = begin
        self._in_tx = False
        self._lock = threading.RLock()

    def _run(self, action: Callable[[Any], _T]) -> _T:
        with self._lock:
            try:
                return action(self.raw.cursor())
            except BaseException:
                if not self._in_tx:
                    self.raw.rollback()
                raise

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Rows the statement changed; 0 when ON CONFLICT DO NOTHING skipped the row."""

        def act(cur: Any) -> int:
            cur.execute(sql, tuple(params))
            return cur.rowcount

        return self._run(act)

    def executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> int:
        """Rows changed across the batch."""

        def act(cur: Any) -> int:
            cur.executemany(sql, [tuple(r) for r in rows])
            return cur.rowcount

        return self._run(act)

    def query_all(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        def act(cur: Any) -> list[tuple[Any, ...]]:
            cur.execute(sql, tuple(params))
            return [tuple(r) for r in cur.fetchall()]

        return self._run(act)

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> tuple[Any, ...] | None:
        rows = self.query_all(sql, params)
        return rows[0] if rows else None

    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        """Commit on clean exit, roll back and re-raise on error. A nested use in the same thread joins the outer one."""
        with self._lock:
            if self._in_tx:
                yield self
                return
            self.raw.cursor().execute(self._begin)
            self._in_tx = True
            try:
                yield self
                self.raw.cursor().execute("COMMIT")
            except BaseException:
                self.raw.cursor().execute("ROLLBACK")
                raise
            finally:
                self._in_tx = False

    def close(self) -> None:
        self.raw.close()


def _sqlite_target(url: str) -> str:
    rest = url[len("sqlite://") :]
    if rest in ("", "/:memory:"):
        return ":memory:"
    if rest.startswith("/") and len(rest) > 1:
        return rest[1:]
    raise ValueError(f"unreadable sqlite URL {url!r}: use sqlite:///<path> or sqlite:///:memory:")


def connect(url: str) -> Connection:
    """Open a connection for a sqlite: or postgresql: URL."""
    if url.startswith("sqlite://"):
        # isolation_level=None: no implicit BEGIN, so a failed statement holds no lock.
        # check_same_thread=False: the Connection's own lock serialises the threads that share it.
        raw = sqlite3.connect(_sqlite_target(url), timeout=30.0, isolation_level=None, check_same_thread=False)
        raw.execute("PRAGMA journal_mode=WAL")
        raw.execute("PRAGMA foreign_keys=ON")
        raw.execute("PRAGMA busy_timeout=30000")
        # IMMEDIATE takes the write lock at BEGIN, where the busy timeout applies,
        # instead of a late read-to-write upgrade that fails SQLITE_BUSY at once.
        return Connection(raw, SQLITE, begin="BEGIN IMMEDIATE")
    if url.startswith(("postgresql://", "postgres://")):
        try:
            import psycopg
            from psycopg.types.string import TextLoader
        except ImportError as exc:
            raise StoreDriverMissing(
                "a postgresql URL needs the psycopg driver: install the postgres extra"
            ) from exc
        raw = psycopg.connect(url, autocommit=True)
        # Hand json columns back as text, as sqlite does, so json_load sees one shape.
        raw.adapters.register_loader("json", TextLoader)
        raw.adapters.register_loader("jsonb", TextLoader)
        return Connection(raw, POSTGRES, begin="BEGIN")
    raise ValueError(f"unsupported store URL {url!r}: accepted schemes are sqlite:// and postgresql://")


def default_url(runs_dir: str | Path) -> str:
    """sqlite URL for cox.db inside `runs_dir`. The directory is not created."""
    return f"sqlite:///{Path(runs_dir) / 'cox.db'}"
