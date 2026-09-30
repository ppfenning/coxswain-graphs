"""Numbered schema migrations, applied when a store is opened.

A migration is a module in the harness package named `store_ddl_NNNN` (four
digits, contiguous from 0001) exposing `VERSION`, `DESCRIPTION` and
`statements(dialect)`. Adding one never edits this file: `default_modules`
finds them with pkgutil. Only `open_store` applies migrations. Read paths call
`check_version`, which never writes.
"""

from __future__ import annotations

import argparse
import importlib
import os
import pkgutil
import re
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from types import ModuleType
from urllib.parse import urlsplit

import harness
from harness.store_dialect import Connection, Dialect, connect

__all__ = [
    "MigrationError",
    "check_version",
    "default_modules",
    "discover_migrations",
    "migrate",
    "open_store",
    "pending",
]

_WITHOUT_BACKUP = "COX_STORE_MIGRATE_WITHOUT_BACKUP"
_BACKUP_DIR_ENV = "COX_STORE_BACKUP_DIR"

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


def pending(conn: Connection, modules: Sequence[ModuleType | str] | None = None) -> list[tuple[int, str]]:
    """(version, description) for every migration newer than the database. Reads only; applies nothing."""
    current = _current_version(conn)
    ordered = _ordered(default_modules() if modules is None else modules)
    return [(m.VERSION, m.DESCRIPTION) for m in ordered if current < m.VERSION]


def _safe_url(url: str) -> str:
    """Scheme and path only: userinfo, host, port and query never reach output. Mirrors store_copy._safe_url."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return f"{url.partition(':')[0]}://"
    return f"{parts.scheme}://{parts.path}"


def _backup_dir() -> Path:
    """`$COX_STORE_BACKUP_DIR` if set, else a fixed path under the user's home, computed here, never hardcoded."""
    override = os.environ.get(_BACKUP_DIR_ENV)
    return Path(override) if override else Path.home() / ".local" / "state" / "coxswain" / "backups"


def _backup_name(dialect: Dialect, current: int, newest: int, now: str) -> str:
    """store-v<current>-to-v<newest>-<UTC yyyymmddThhmmssZ>.<ext>. The timestamp is `now`, not a fresh clock read."""
    ts = now.replace("-", "").replace(":", "")
    ext = "sqlite" if dialect.name == "sqlite" else "dump"
    return f"store-v{current}-to-v{newest}-{ts}.{ext}"


def _sqlite_backup(conn: Connection, dest: Path) -> None:
    """Back up the live connection, not a fresh reconnect: a `sqlite:///:memory:` store has no file to reopen."""
    target = sqlite3.connect(str(dest))
    try:
        conn.raw.backup(target)
    finally:
        target.close()


def _pg_dump(url: str, dest: Path, safe_url: str) -> None:
    """`pg_dump -Fc` the store at `url` into `dest`. Never puts `url` or the process's own output in a message."""
    if shutil.which("pg_dump") is None:
        raise MigrationError(
            f"pg_dump is not on PATH; cannot back up {safe_url} before migrating. "
            f"Install pg_dump, or set {_WITHOUT_BACKUP}=1 to migrate without a backup."
        )
    result = subprocess.run(
        ["pg_dump", "-Fc", f"--dbname={url}", "-f", str(dest)], capture_output=True, check=False
    )
    if result.returncode != 0:
        raise MigrationError(
            f"pg_dump exited {result.returncode} backing up {safe_url}. "
            f"Set {_WITHOUT_BACKUP}=1 to migrate without a backup."
        )


def _backup_before_migrate(conn: Connection, url: str, current: int, newest: int, now: str) -> None:
    """Write a backup of `url` unless the override skips it. Never calls `migrate`; the caller does that next."""
    if os.environ.get(_WITHOUT_BACKUP) == "1":
        print(f"WARNING: {_WITHOUT_BACKUP}=1 is set; migrating without a backup.", file=sys.stderr)
        return
    safe_url = _safe_url(url)
    directory = _backup_dir()
    directory.mkdir(parents=True, exist_ok=True)
    dest = directory / _backup_name(conn.dialect, current, newest, now)
    if conn.dialect.name == "sqlite":
        _sqlite_backup(conn, dest)
    else:
        _pg_dump(url, dest, safe_url)


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
    """Connect, back up first when a migration is pending, bring the schema up to date, and return the connection.

    `now` is the applied-at text. When the database is already at the newest known migration,
    neither a backup file nor a subprocess is written or spawned.
    """
    conn = connect(url)
    try:
        current = _current_version(conn)
        newest = len(_ordered(default_modules()))
        if current < newest:
            _backup_before_migrate(conn, url, current, newest, now)
        migrate(conn, now)
    except BaseException:
        conn.close()
        raise
    return conn


def main(argv: Sequence[str]) -> int:
    ap = argparse.ArgumentParser(prog="python -m harness.store_migrate", description=__doc__.split("\n")[0])
    ap.add_argument("--pending", metavar="URL", required=True, help="list migrations newer than this store's version")
    args = ap.parse_args(argv)
    conn = connect(args.pending)
    try:
        current, newest = check_version(conn)
        if current > newest:
            print(f"database is at schema version {current}, newest known migration is {newest}", file=sys.stderr)
            return 2
        items = pending(conn)
        if not items:
            print(f"up to date (v{current})")
            return 0
        for version, description in items:
            print(f"{version:04d} {description}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
