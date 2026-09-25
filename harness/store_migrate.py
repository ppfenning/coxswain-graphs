"""Numbered schema migrations, applied when a store is opened.

A migration is a module in the harness package named `store_ddl_NNNN` (four
digits, contiguous from 0001) exposing `VERSION`, `DESCRIPTION` and
`statements(dialect)`. Adding one never edits this file: `default_modules`
finds them with pkgutil. Only `open_store` applies migrations. Read paths call
`check_version`, which never writes.
"""

from __future__ import annotations

import importlib
import pkgutil
import re
from collections.abc import Callable, Sequence
from types import ModuleType

import harness
from harness.store_dialect import Connection, Dialect, connect

__all__ = [
    "MigrationError",
    "check_version",
    "default_modules",
    "discover_migrations",
    "migrate",
    "open_store",
]

_NAME = re.compile(r"(?:^|\.)store_ddl_(\d{4})$")
_CREATE = "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at TEXT, description TEXT)"

Migration = tuple[int, Callable[[Dialect], tuple[str, ...]]]


class MigrationError(RuntimeError):
    """The migration set or the database is not in a state the runner will act on."""


def _ordered(given: Sequence[ModuleType | str]) -> tuple[ModuleType, ...]:
    """Modules, or dotted names to import, sorted by VERSION, each matching its name, contiguous from 1."""
    modules = tuple(importlib.import_module(g) if isinstance(g, str) else g for g in given)
    for m in modules:
        found = _NAME.search(m.__name__)
        if found is None:
            raise MigrationError(f"{m.__name__} is not named store_ddl_NNNN")
        named = int(found.group(1))
        if named != m.VERSION:
            raise MigrationError(f"{m.__name__} declares VERSION {m.VERSION}, its name says {named}")
    ordered = tuple(sorted(modules, key=lambda m: m.VERSION))
    versions = [m.VERSION for m in ordered]
    dupes = sorted({v for v in versions if versions.count(v) > 1})
    if dupes:
        raise MigrationError(f"duplicate migration number {dupes[0]}")
    gaps = [(want, got) for want, got in zip(range(1, len(versions) + 1), versions) if want != got]
    if gaps:
        raise MigrationError(f"migration numbering has a gap: expected {gaps[0][0]}, found {gaps[0][1]}")
    return ordered


def discover_migrations(modules: Sequence[ModuleType | str]) -> tuple[Migration, ...]:
    """(version, statements) pairs in version order; a gap or duplicate raises."""
    return tuple((m.VERSION, m.statements) for m in _ordered(modules))


def default_modules() -> tuple[ModuleType, ...]:
    """Every store_ddl_NNNN module found in the harness package."""
    names = sorted(i.name for i in pkgutil.iter_modules(harness.__path__) if re.fullmatch(r"store_ddl_\d{4}", i.name))
    return tuple(importlib.import_module(f"harness.{n}") for n in names)


def _has_version_table(conn: Connection) -> bool:
    # unknown: the postgres branch is not run in this repository's tests.
    if conn.dialect.name == "sqlite":
        sql = "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
    else:
        sql = "SELECT 1 FROM information_schema.tables WHERE table_name = 'schema_version' AND table_schema = current_schema()"
    return conn.query_one(sql) is not None


def _current_version(conn: Connection) -> int:
    if not _has_version_table(conn):
        return 0
    row = conn.query_one("SELECT MAX(version) FROM schema_version")
    return 0 if row is None or row[0] is None else int(row[0])


def _lock_versions(dialect: Dialect) -> tuple[str, ...]:
    # sqlite's BEGIN IMMEDIATE already holds the write lock. Postgres BEGIN takes none,
    # so a second opener would read schema_version before the first commits.
    return ("LOCK TABLE schema_version IN SHARE ROW EXCLUSIVE MODE",) if dialect.name == "postgres" else ()


def _steps(
    m: ModuleType, dialect: Dialect, record: str, applied_at: str, current: int
) -> tuple[tuple[str, tuple[object, ...]], ...]:
    """The migration's statements then its schema_version row; none when `current` already covers it."""
    return (
        ()
        if current >= m.VERSION
        else (*((sql, ()) for sql in m.statements(dialect)), (record, (m.VERSION, applied_at, m.DESCRIPTION)))
    )


def check_version(conn: Connection, modules: Sequence[ModuleType | str] | None = None) -> tuple[int, int]:
    """(current, expected) versions. Reads only; applies nothing. `modules` defaults to the harness package's."""
    return _current_version(conn), len(_ordered(default_modules() if modules is None else modules))


def migrate(conn: Connection, applied_at: str, modules: Sequence[ModuleType | str] | None = None) -> int:
    """Apply each migration newer than the database, one transaction apiece. Returns the version read back.

    The version read before the loop only skips work. Each transaction reads it again under
    the write lock, so a migration another opener committed meanwhile is not run twice.
    """
    ordered = _ordered(default_modules() if modules is None else modules)
    newest = len(ordered)
    before = _current_version(conn)
    if before > newest:
        raise MigrationError(f"database is at schema version {before}, newest known migration is {newest}")
    conn.execute(_CREATE)
    p = conn.dialect.placeholder
    record = f"INSERT INTO schema_version (version, applied_at, description) VALUES ({p}, {p}, {p})"
    for m in ordered[before:]:
        with conn.transaction():
            for sql in _lock_versions(conn.dialect):
                conn.execute(sql)
            for sql, params in _steps(m, conn.dialect, record, applied_at, _current_version(conn)):
                conn.execute(sql, params)
    reached = _current_version(conn)
    if reached != newest:
        raise MigrationError(f"database is at schema version {reached} after migrating, expected {newest}")
    return reached


def open_store(url: str, now: str) -> Connection:
    """Connect, bring the schema up to date, and return the connection. `now` is the applied-at text."""
    conn = connect(url)
    try:
        migrate(conn, now)
    except BaseException:
        conn.close()
        raise
    return conn
