import dataclasses
import sqlite3
import sys
import threading
import time
import types

import pytest

from harness.store_dialect import (
    POSTGRES,
    SQLITE,
    StoreDriverMissing,
    connect,
    default_url,
    forbidden_constructs,
    insert_ignore,
    json_load,
    json_text,
    upsert,
)


def test_dialect_values_are_literal_and_frozen():
    assert (SQLITE.name, SQLITE.placeholder, SQLITE.json_type, SQLITE.bool_type) == ("sqlite", "?", "TEXT", "SMALLINT")
    assert (POSTGRES.name, POSTGRES.placeholder, POSTGRES.json_type, POSTGRES.bool_type) == (
        "postgres",
        "%s",
        "JSONB",
        "SMALLINT",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        SQLITE.name = "x"  # type: ignore[misc]


def test_insert_ignore_text_per_dialect():
    assert insert_ignore(SQLITE, "runs", ["id", "body"], ["id"]) == (
        "INSERT INTO runs (id, body) VALUES (?, ?) ON CONFLICT (id) DO NOTHING"
    )
    assert insert_ignore(POSTGRES, "runs", ["id", "body"], ["id"]) == (
        "INSERT INTO runs (id, body) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING"
    )


def test_upsert_text_per_dialect():
    assert upsert(SQLITE, "runs", ["id", "a", "b"], ["id"]) == (
        "INSERT INTO runs (id, a, b) VALUES (?, ?, ?) ON CONFLICT (id) DO UPDATE SET a = excluded.a, b = excluded.b"
    )
    assert upsert(POSTGRES, "runs", ["id", "a"], ["id"]) == (
        "INSERT INTO runs (id, a) VALUES (%s, %s) ON CONFLICT (id) DO UPDATE SET a = excluded.a"
    )


def test_upsert_with_only_key_columns_does_nothing_on_conflict():
    assert upsert(SQLITE, "t", ["a", "b"], ["a", "b"]) == (
        "INSERT INTO t (a, b) VALUES (?, ?) ON CONFLICT (a, b) DO NOTHING"
    )


def test_upsert_without_keys_is_refused():
    with pytest.raises(ValueError):
        upsert(SQLITE, "t", ["a"], [])


@pytest.mark.parametrize(
    "sql,token",
    [
        ("CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT)", "AUTOINCREMENT"),
        ("pragma journal_mode=WAL", "PRAGMA"),
        ("INSERT  OR   REPLACE INTO t VALUES (1)", "INSERT OR REPLACE"),
        ("INSERT /* x */ OR REPLACE INTO t VALUES (1)", "INSERT OR REPLACE"),
        ("REPLACE INTO t VALUES (1)", "INSERT OR REPLACE"),
        ("SELECT strftime('%s', ts) FROM t", "strftime"),
        ("SELECT json_extract(body, '$.a') FROM t", "json_extract"),
    ],
)
def test_forbidden_constructs_flags_each_banned_token(sql, token):
    assert forbidden_constructs(sql) == (token,)


def test_forbidden_constructs_reports_several_once_in_fixed_order():
    sql = "PRAGMA x; INSERT OR REPLACE INTO t VALUES (1); pragma y; SELECT strftime('%s', 1)"
    assert forbidden_constructs(sql) == ("PRAGMA", "INSERT OR REPLACE", "strftime")


def test_forbidden_constructs_passes_clean_sql():
    sql = (
        "INSERT INTO t (a) VALUES (?) ON CONFLICT (a) DO NOTHING; SELECT pragmatic, strftime, replace(a, 'x', 'y') FROM t"
        " -- no PRAGMA here\n WHERE note = 'json_extract(x) or AUTOINCREMENT' /* INSERT OR REPLACE */"
    )
    assert forbidden_constructs(sql) == ()


def test_json_text_is_sorted_and_compact_and_round_trips():
    value = {"b": [1, 2], "a": {"z": None}}
    assert json_text(value) == '{"a":{"z":null},"b":[1,2]}'
    assert json_load(json_text(value)) == value
    assert json_load(b'"scalar"') == "scalar"
    assert json_load(None) is None


def test_default_url_names_cox_db_inside_the_directory(tmp_path):
    assert default_url(tmp_path) == f"sqlite:///{tmp_path}/cox.db"


def test_in_memory_sqlite_round_trips_a_json_column():
    for url in ("sqlite://", "sqlite:///:memory:"):
        conn = connect(url)
        assert conn.dialect is SQLITE
        conn.execute(f"CREATE TABLE r (id INTEGER PRIMARY KEY, body {SQLITE.json_type}, ok {SQLITE.bool_type})")
        sql = insert_ignore(SQLITE, "r", ["id", "body", "ok"], ["id"])
        assert conn.execute(sql, (1, json_text({"k": [1, "x"]}), 1)) == 1
        assert conn.execute(sql, (1, json_text({"k": "dup"}), 0)) == 0
        assert json_load(conn.query_one("SELECT body FROM r WHERE id = ?", (1,))[0]) == {"k": [1, "x"]}
        assert conn.query_all("SELECT id, ok FROM r") == [(1, 1)]
        assert conn.query_one("SELECT id FROM r WHERE id = ?", (9,)) is None


def test_a_failed_transaction_leaves_no_row_and_a_good_one_commits():
    conn = connect("sqlite://")
    conn.execute("CREATE TABLE r (id INTEGER PRIMARY KEY)")
    with pytest.raises(RuntimeError), conn.transaction():
        conn.execute("INSERT INTO r (id) VALUES (?)", (1,))
        raise RuntimeError("boom")
    assert conn.query_all("SELECT id FROM r") == []
    with conn.transaction():
        assert conn.executemany("INSERT INTO r (id) VALUES (?)", [(2,), (3,)]) == 2
    assert conn.query_all("SELECT id FROM r ORDER BY id") == [(2,), (3,)]


def test_a_failed_statement_outside_a_transaction_holds_no_lock(tmp_path):
    conn = connect(default_url(tmp_path))
    conn.execute("CREATE TABLE p (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE c (id INTEGER PRIMARY KEY, p INTEGER REFERENCES p (id))")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO c (id, p) VALUES (?, ?)", (1, 99))
    assert conn.raw.in_transaction is False
    other = connect(default_url(tmp_path))
    other.raw.execute("PRAGMA busy_timeout=0")
    assert other.execute("INSERT INTO p (id) VALUES (?)", (1,)) == 1
    assert conn.execute("INSERT INTO c (id, p) VALUES (?, ?)", (1, 1)) == 1
    assert conn.query_all("SELECT id, p FROM c") == [(1, 1)]


def test_a_contended_transaction_waits_for_the_lock_instead_of_failing(tmp_path):
    holder = connect(default_url(tmp_path))
    holder.execute("CREATE TABLE r (id INTEGER PRIMARY KEY)")
    errors = []

    def write():
        try:
            waiter = connect(default_url(tmp_path))
            with waiter.transaction():
                waiter.query_all("SELECT id FROM r")
                waiter.execute("INSERT INTO r (id) VALUES (?)", (2,))
        except Exception as exc:  # reported to the main thread below
            errors.append(exc)

    with holder.transaction():
        holder.execute("INSERT INTO r (id) VALUES (?)", (1,))
        thread = threading.Thread(target=write)
        thread.start()
        time.sleep(0.3)
    thread.join(timeout=10)
    assert errors == []
    assert holder.query_all("SELECT id FROM r ORDER BY id") == [(1,), (2,)]


def test_file_sqlite_opens_with_wal_foreign_keys_and_busy_timeout(tmp_path):
    conn = connect(default_url(tmp_path))
    assert (tmp_path / "cox.db").exists()
    assert conn.query_one("PRAGMA journal_mode")[0] == "wal"
    assert conn.query_one("PRAGMA foreign_keys")[0] == 1
    assert conn.query_one("PRAGMA busy_timeout")[0] > 0


def test_postgres_url_without_the_driver_names_the_extra(monkeypatch):
    monkeypatch.setitem(sys.modules, "psycopg", None)
    with pytest.raises(StoreDriverMissing, match="postgres extra"):
        connect("postgresql://u@h/db")


class _FakePgConnection:
    """Stands in for a psycopg connection: records SQL, fails any statement starting BAD."""

    def __init__(self):
        self.sql = []
        self.loaders = {}
        self.rollbacks = 0
        self.adapters = self

    def register_loader(self, name, loader):
        self.loaders[name] = loader

    def cursor(self):
        return _FakePgCursor(self)

    def rollback(self):
        self.rollbacks += 1


class _FakePgCursor:
    rowcount = 1

    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params=()):
        self.conn.sql.append(sql)
        if sql.startswith("BAD"):
            raise RuntimeError("statement failed")


def test_postgres_url_with_a_driver_opens_autocommit_with_json_as_text(monkeypatch):
    raw = _FakePgConnection()
    calls = []
    psycopg = types.ModuleType("psycopg")
    psycopg.connect = lambda url, **kw: calls.append((url, kw)) or raw
    string_mod = types.ModuleType("psycopg.types.string")
    string_mod.TextLoader = object()
    monkeypatch.setitem(sys.modules, "psycopg", psycopg)
    monkeypatch.setitem(sys.modules, "psycopg.types", types.ModuleType("psycopg.types"))
    monkeypatch.setitem(sys.modules, "psycopg.types.string", string_mod)

    conn = connect("postgresql://u@h/db")
    assert conn.dialect is POSTGRES
    assert calls == [("postgresql://u@h/db", {"autocommit": True})]
    assert raw.loaders == {"json": string_mod.TextLoader, "jsonb": string_mod.TextLoader}
    assert conn.execute(insert_ignore(POSTGRES, "r", ["id"], ["id"]), (1,)) == 1
    with pytest.raises(RuntimeError):
        conn.execute("BAD")
    assert raw.rollbacks == 1
    with pytest.raises(RuntimeError), conn.transaction():
        conn.execute("BAD")
    assert raw.sql[-3:] == ["BEGIN", "BAD", "ROLLBACK"]
    with conn.transaction():
        conn.execute("SELECT 1")
    assert raw.sql[-3:] == ["BEGIN", "SELECT 1", "COMMIT"]


def test_an_unknown_scheme_is_refused():
    with pytest.raises(ValueError, match="sqlite://"):
        connect("mysql://h/db")
