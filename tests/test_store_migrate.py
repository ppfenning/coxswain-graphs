import sqlite3
import sys
from contextlib import contextmanager
from types import ModuleType

import pytest

import harness.store_migrate as store_migrate
from harness.store_dialect import POSTGRES, SQLITE, Connection, connect
from harness.store_migrate import (
    MigrationError,
    check_version,
    default_modules,
    discover_migrations,
    migrate,
    open_store,
    pending,
)

NOW = "2026-09-24T00:00:00Z"


def fake(number, statements, name=None):
    m = ModuleType(name or f"harness.store_ddl_{number:04d}")
    m.VERSION = number
    m.DESCRIPTION = f"fake {number}"
    m.statements = lambda dialect: tuple(statements)
    return m


ONE = ("CREATE TABLE t1 (id INTEGER PRIMARY KEY)",)
TWO = ("CREATE TABLE t2 (id INTEGER PRIMARY KEY)", "INSERT INTO t2 (id) VALUES (1)")


@pytest.fixture
def conn(store_url):
    """An unmigrated store on either backend; open_store would migrate it first."""
    c = connect(store_url)
    yield c
    c.close()


def tables(c):
    if c.dialect is POSTGRES:
        sql = "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
    else:
        sql = "SELECT name FROM sqlite_master WHERE type = 'table'"
    return {r[0] for r in c.query_all(sql)}


def missing_table_error(c):
    """The exception each driver raises for a statement naming a table that does not exist."""
    if c.dialect is POSTGRES:
        from psycopg.errors import UndefinedTable

        return UndefinedTable
    return sqlite3.OperationalError


def test_empty_database_applies_both_in_order_and_lands_at_two(conn):
    assert migrate(conn, NOW, [fake(2, TWO), fake(1, ONE)]) == 2
    assert conn.query_all("SELECT version, applied_at, description FROM schema_version ORDER BY version") == [
        (1, NOW, "fake 1"),
        (2, NOW, "fake 2"),
    ]
    assert {"t1", "t2"} <= tables(conn)


def test_database_at_one_applies_only_the_second(conn):
    migrate(conn, "earlier", [fake(1, ONE)])
    migrate(conn, NOW, [fake(1, ("CREATE TABLE never (id INTEGER)",)), fake(2, TWO)])
    assert "never" not in tables(conn)
    assert conn.query_all("SELECT version, applied_at FROM schema_version ORDER BY version") == [
        (1, "earlier"),
        (2, NOW),
    ]


def test_a_second_migrate_changes_nothing(conn):
    mods = [fake(1, ONE), fake(2, TWO)]
    migrate(conn, NOW, mods)
    before = conn.query_all("SELECT * FROM schema_version ORDER BY version")
    assert migrate(conn, "later", mods) == 2
    assert conn.query_all("SELECT * FROM schema_version ORDER BY version") == before
    assert conn.query_all("SELECT id FROM t2") == [(1,)]


def test_a_failing_statement_rolls_back_that_migration_and_keeps_version_one(conn):
    bad = fake(2, ("CREATE TABLE t2 (id INTEGER PRIMARY KEY)", "INSERT INTO missing_table VALUES (1)"))
    with pytest.raises(missing_table_error(conn), match="missing_table"):
        migrate(conn, NOW, [fake(1, ONE), bad])
    assert conn.query_all("SELECT version FROM schema_version") == [(1,)]
    assert "t2" not in tables(conn)
    assert "t1" in tables(conn)


class Interleaved(Connection):
    """Runs `hook` once, just before this connection's first BEGIN: another opener racing it."""

    def __init__(self, raw, hook):
        super().__init__(raw, SQLITE, begin="BEGIN IMMEDIATE")
        self.hook = hook

    @contextmanager
    def transaction(self):
        hook, self.hook = self.hook, lambda: None
        hook()
        with super().transaction() as c:
            yield c


# SQLite only: two file connections and BEGIN IMMEDIATE, which Postgres does not have.
def test_a_concurrent_opener_that_migrates_first_is_not_migrated_over(tmp_path):
    url = f"sqlite:///{tmp_path / 'race.db'}"
    mods = [fake(1, ONE), fake(2, TWO)]
    other = connect(url)
    racer = Interleaved(connect(url).raw, lambda: migrate(other, "other", mods))
    try:
        assert migrate(racer, NOW, mods) == 2
        assert racer.query_all("SELECT version, applied_at FROM schema_version ORDER BY version") == [
            (1, "other"),
            (2, "other"),
        ]
    finally:
        other.close()
        racer.close()


def test_a_gap_in_numbering_is_refused(conn):
    with pytest.raises(MigrationError, match="expected 2, found 3"):
        migrate(conn, NOW, [fake(1, ONE), fake(3, TWO)])
    assert "schema_version" not in tables(conn)


def test_a_duplicate_number_is_refused():
    with pytest.raises(MigrationError, match="duplicate migration number 1"):
        discover_migrations([fake(1, ONE), fake(1, TWO, name="harness.store_ddl_0001")])


def test_a_version_that_disagrees_with_its_name_is_refused():
    with pytest.raises(MigrationError, match="VERSION 2"):
        discover_migrations([fake(2, ONE, name="harness.store_ddl_0001")])


def test_discover_returns_ordered_version_statement_pairs():
    pairs = discover_migrations([fake(2, TWO), fake(1, ONE)])
    assert [v for v, _ in pairs] == [1, 2]
    assert pairs[0][1](None) == ONE


def test_discover_accepts_dotted_names(monkeypatch):
    monkeypatch.setitem(sys.modules, "fakepkg.store_ddl_0001", fake(1, ONE, name="fakepkg.store_ddl_0001"))
    assert [v for v, _ in discover_migrations(["fakepkg.store_ddl_0001"])] == [1]


def test_a_database_newer_than_the_code_is_refused_before_any_write(conn, monkeypatch):
    migrate(conn, NOW, [fake(1, ONE), fake(2, TWO), fake(3, ("SELECT 1",))])
    executed = []

    def watch(name):
        real = getattr(conn, name)

        def spy(*args, **kwargs):
            executed.append(name)
            return real(*args, **kwargs)

        monkeypatch.setattr(conn, name, spy)

    for name in ("execute", "executemany", "transaction"):  # every way this connection writes
        watch(name)
    with pytest.raises(MigrationError) as err:
        migrate(conn, NOW, [fake(1, ONE), fake(2, TWO)])
    assert str(err.value) == "database is at schema version 3, newest known migration is 2"
    assert executed == []


def test_check_version_applies_nothing(conn):
    mods = [fake(1, ONE), fake(2, TWO)]
    assert check_version(conn, mods) == (0, 2)
    assert tables(conn) == set()
    migrate(conn, NOW, mods[:1])
    assert check_version(conn, mods) == (1, 2)
    assert conn.query_all("SELECT version FROM schema_version") == [(1,)]
    assert "t2" not in tables(conn)


def test_open_store_leaves_the_store_at_the_newest_real_migration(store_conn):
    newest = len(default_modules())
    assert check_version(store_conn) == (newest, newest)


def test_a_store_migrated_from_version_two_reads_an_existing_run_with_host_null(conn):
    real = default_modules()
    assert migrate(conn, NOW, real[:2]) == 2
    conn.execute("INSERT INTO runs (run_id, status) VALUES ('r1', 'done')")
    assert migrate(conn, NOW, real) == len(real)
    assert conn.query_all("SELECT run_id, status, host FROM runs") == [("r1", "done", None)]


def test_a_store_migrated_from_version_nine_reads_an_existing_run_and_lease_with_paused_and_status_null(conn):
    real = default_modules()
    assert migrate(conn, NOW, real[:9]) == 9
    conn.execute("INSERT INTO runs (run_id, status) VALUES ('r1', 'done')")
    conn.execute(
        "INSERT INTO leases (name, holder, epoch, heartbeat_at, expires_at) VALUES ('l1', 'h1', 1, 'T0', 'T1')"
    )
    assert migrate(conn, NOW, real) == len(real)
    assert conn.query_all("SELECT run_id, paused_at FROM runs") == [("r1", None)]
    assert conn.query_all("SELECT name, status FROM leases") == [("l1", None)]


def test_a_fresh_store_is_at_the_newest_version_and_runs_has_a_host_column(store_conn):
    newest = len(default_modules())
    assert check_version(store_conn) == (newest, newest)
    assert store_conn.query_all("SELECT host FROM runs") == []
    assert store_conn.query_all("SELECT paused_at FROM runs") == []
    assert store_conn.query_all("SELECT status FROM leases") == []


# SQLite only: the same check through a file path, which is how production opens its store.
def test_open_store_on_a_file_leaves_the_store_at_the_newest_real_migration(tmp_path):
    c = open_store(f"sqlite:///{tmp_path / 'x.db'}", NOW)
    try:
        newest = len(default_modules())
        assert check_version(c) == (newest, newest)
    finally:
        c.close()


class FakePG(Connection):
    """A postgres-shaped connection that answers `_current_version` (as version 0) but refuses any write."""

    def __init__(self):
        super().__init__(raw=None, dialect=POSTGRES, begin="BEGIN")

    def query_all(self, sql, params=()):
        return []

    def execute(self, sql, params=()):
        raise AssertionError("must not write when the backup step failed")

    def transaction(self):
        raise AssertionError("must not write when the backup step failed")

    def close(self):
        pass


def test_open_store_backs_up_the_sqlite_file_before_the_new_table_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("COX_STORE_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setattr(store_migrate, "default_modules", lambda: (fake(1, ONE),))
    url = f"sqlite:///{tmp_path / 'x.db'}"
    open_store(url, NOW).close()
    backups = list((tmp_path / "backups").glob("*.sqlite"))
    assert len(backups) == 1
    backup = sqlite3.connect(str(backups[0]))
    try:
        assert backup.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall() == []
    finally:
        backup.close()


def test_open_store_with_nothing_pending_writes_no_backup_file(tmp_path, monkeypatch):
    monkeypatch.setenv("COX_STORE_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setattr(store_migrate, "default_modules", lambda: (fake(1, ONE),))
    url = f"sqlite:///{tmp_path / 'x.db'}"
    open_store(url, NOW).close()
    after_first_open = list((tmp_path / "backups").glob("*"))
    assert len(after_first_open) == 1
    open_store(url, NOW).close()
    assert list((tmp_path / "backups").glob("*")) == after_first_open


def test_a_missing_pg_dump_raises_and_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))  # an empty directory: pg_dump is not on it
    monkeypatch.setattr(store_migrate, "connect", lambda url: FakePG())
    with pytest.raises(MigrationError, match="pg_dump"):
        open_store("postgresql://user:secret@host/db", NOW)


def test_the_without_backup_override_skips_the_backup_and_prints_one_warning_line(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("COX_STORE_MIGRATE_WITHOUT_BACKUP", "1")
    monkeypatch.setenv("COX_STORE_BACKUP_DIR", str(tmp_path / "backups"))
    url = f"sqlite:///{tmp_path / 'x.db'}"
    open_store(url, NOW).close()
    assert not (tmp_path / "backups").exists()
    assert capsys.readouterr().err.count("\n") == 1


def test_pending_lists_exactly_the_migrations_newer_than_current(conn):
    migrate(conn, NOW, [fake(1, ONE)])
    assert pending(conn, [fake(1, ONE), fake(2, TWO), fake(3, ("SELECT 1",))]) == [(2, "fake 2"), (3, "fake 3")]


def test_a_password_in_the_url_never_appears_in_a_raised_error(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))  # an empty directory: pg_dump is not on it
    monkeypatch.setattr(store_migrate, "connect", lambda url: FakePG())
    url = "postgresql://user:s3cret-pw@host/db"
    with pytest.raises(MigrationError) as err:
        open_store(url, NOW)
    assert "s3cret-pw" not in str(err.value)
