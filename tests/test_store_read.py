import ast
import os
import sqlite3
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

import harness.store_ddl_0001 as ddl1
import harness.store_read as read
from harness.store_dialect import POSTGRES, Connection, _sqlite_target, connect, forbidden_constructs, json_text
from harness.store_migrate import migrate, open_store

NOW = "2026-09-24T00:00:00Z"


def put(conn, table, **row):
    cols = list(row)
    marks = ", ".join(conn.dialect.placeholder for _ in cols)
    conn.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({marks})", tuple(row.values()))


def call(conn, call_id, run_id, seq, role, alias, tier, cost, inp, cache, out, turns=1, ok=1, **extra):
    put(
        conn, "node_calls", call_id=call_id, run_id=run_id, seq=seq, role=role, model_alias=alias, tier=tier,
        cost_usd=cost, input_total=inp, cache_read_tokens=cache, output_tokens=out, turns=turns, ok=ok,
        ts=f"2026-09-24T00:00:0{seq}Z", **extra,
    )  # fmt: skip


def seed(c):
    put(c, "runs", run_id="r1", principal="pat", status="done", started_at="2026-09-24T01:00:00Z", graph_id="g1")
    put(c, "runs", run_id="r2", principal="pat", status="done", started_at="2026-09-24T02:00:00Z")
    call(c, "c1", "r1", 1, "plan", "sonnet", "mid", 0.5, 1000, 250, 100, turns=2, decision_json=json_text({"why": "x"}))
    call(c, "c2", "r1", 2, "build", "sonnet", "mid", 1.0, 3000, 750, 300, turns=3, node_id="n1")
    call(c, "c3", "r1", 3, "build", "haiku", "low", 0.25, 0, 0, 50)
    call(c, "c4", "r2", 1, "plan", "sonnet", "mid", 2.0, 0, 0, 10)
    put(c, "attempts", run_id="r1", task_id="t1", seq=2, kind="retry", reason="lint")
    put(c, "attempts", run_id="r1", task_id="t1", seq=1, kind="first", reason="start")
    put(c, "ledger", row_hash="h2", run_id="r1", ts="b", epoch=2, row_json=json_text({"n": 2}))
    put(c, "ledger", row_hash="h1", run_id="r1", ts="a", epoch=1, row_json=json_text({"n": 1}))
    put(c, "gate_decisions", run_id="r1", phase_id="p1", seq=1, decision="approve", applied=1, detail_json=json_text({"a": 1}))
    put(c, "leases", name="epic", holder="me", epoch=7, heartbeat_at="h", expires_at="e")
    put(c, "graphs", graph_id="g1", name="review", version="1", content_hash="g1", registered_at=NOW, definition_json="{}")
    put(c, "graph_nodes", graph_id="g1", node_id="b", ord=1, role="build")
    put(c, "graph_nodes", graph_id="g1", node_id="a", ord=0, role="plan", default_tier="mid")
    put(c, "graph_edges", graph_id="g1", src="a", dst="b")
    return c


@pytest.fixture
def conn(store_conn):
    return seed(store_conn)


@pytest.fixture
def sqlite_conn():
    c = open_store("sqlite:///:memory:", NOW)
    yield seed(c)
    c.close()


def test_run_summary_totals_and_cache_share(conn):
    assert read.run_summary(conn, "r1") == {
        "run_id": "r1", "calls": 3, "cost_usd": 1.75, "turns": 6, "input_total": 4000,
        "cache_read_tokens": 1000, "output_tokens": 450, "cache_share": 0.25,
    }  # fmt: skip


def test_cache_share_is_none_when_input_total_is_zero(conn):
    s = read.run_summary(conn, "r2")
    assert (s["calls"], s["cost_usd"], s["input_total"], s["cache_share"]) == (1, 2.0, 0, None)


def test_an_unknown_run_has_no_summary_and_a_run_with_no_calls_sums_to_zero(conn):
    assert read.run_summary(conn, "nope") is None
    put(conn, "runs", run_id="r3", status="running", started_at="2026-09-24T03:00:00Z")
    s = read.run_summary(conn, "r3")
    assert (s["calls"], s["cost_usd"], s["turns"], s["cache_share"]) == (0, 0.0, 0, None)


def test_postgres_decimal_sums_come_back_as_int_and_float():
    s = read.summary_row("r", (2, Decimal("1.5"), Decimal(3), Decimal(400), Decimal(100), Decimal(9)))
    assert s == {
        "run_id": "r", "calls": 2, "cost_usd": 1.5, "turns": 3, "input_total": 400,
        "cache_read_tokens": 100, "output_tokens": 9, "cache_share": 0.25,
    }  # fmt: skip
    assert [type(v) for v in (s["cost_usd"], s["input_total"], s["cache_share"])] == [float, int, float]
    m = read.model_row(("sonnet", "mid", 1, Decimal("0.5"), Decimal(10), Decimal(2)))
    assert (type(m["cost_usd"]), type(m["input_total"])) == (float, int)


def test_cost_by_model_groups_by_alias_and_tier_within_one_run(conn):
    assert read.cost_by_model(conn, "r1") == [
        {"model_alias": "haiku", "tier": "low", "calls": 1, "cost_usd": 0.25, "input_total": 0, "output_tokens": 50},
        {"model_alias": "sonnet", "tier": "mid", "calls": 2, "cost_usd": 1.5, "input_total": 4000, "output_tokens": 400},
    ]
    assert [r["cost_usd"] for r in read.cost_by_model(conn, "r2")] == [2.0]


def test_calls_are_in_seq_order_filtered_by_role_with_json_decoded(conn):
    assert [c["call_id"] for c in read.calls(conn, "r1")] == ["c1", "c2", "c3"]
    assert [c["call_id"] for c in read.calls(conn, "r1", "build")] == ["c2", "c3"]
    first = read.calls(conn, "r1")[0]
    assert (first["decision_json"], first["detail_json"], first["node_id"]) == ({"why": "x"}, None, None)


def test_list_runs_is_newest_first_since_and_limited(conn):
    assert [r["run_id"] for r in read.list_runs(conn, "2026-09-24T00:00:00Z", 10)] == ["r2", "r1"]
    assert [r["run_id"] for r in read.list_runs(conn, "2026-09-24T00:00:00Z", 1)] == ["r2"]
    assert [r["run_id"] for r in read.list_runs(conn, "2026-09-24T01:30:00Z", 10)] == ["r2"]


def test_attempts_ledger_gate_and_lease(conn):
    assert [a["kind"] for a in read.attempts(conn, "t1")] == ["first", "retry"]
    assert [r["row_json"] for r in read.ledger_rows(conn, "r1")] == [{"n": 1}, {"n": 2}]
    (g,) = read.gate_decisions(conn, "r1")
    assert (g["decision"], g["applied"], g["detail_json"]) == ("approve", 1, {"a": 1})
    assert read.current_lease(conn, "epic") == {
        "name": "epic", "holder": "me", "epoch": 7, "heartbeat_at": "h", "expires_at": "e",
    }  # fmt: skip
    assert read.current_lease(conn, "other") is None


def test_run_graph_returns_the_registered_nodes_in_ord_order_and_the_edges(conn):
    g = read.run_graph(conn, "r1")
    assert (g["name"], g["version"]) == ("review", "1")
    assert [(n["node_id"], n["role"], n["default_tier"]) for n in g["nodes"]] == [("a", "plan", "mid"), ("b", "build", None)]
    assert g["edges"] == [{"src": "a", "dst": "b"}]


def test_run_graph_is_none_without_a_graph_id_or_a_registration(conn):
    assert read.run_graph(conn, "r2") is None
    assert read.run_graph(conn, "nope") is None


# SQLite only, down to the WAL tests: connect_readonly opens a file path with mode=ro, which Postgres has no analogue for.
def test_connect_readonly_accepts_a_current_database(tmp_path):
    url = f"sqlite:///{tmp_path / 'cox.db'}"
    open_store(url, NOW).close()
    conn = read.connect_readonly(url)
    assert read.list_runs(conn, "", 5) == []
    conn.close()


def test_connect_readonly_refuses_an_older_database_in_one_line(tmp_path):
    url = f"sqlite:///{tmp_path / 'cox.db'}"
    c = connect(url)
    migrate(c, NOW, modules=[ddl1])
    c.close()
    with pytest.raises(read.StoreVersionError) as err:
        read.connect_readonly(url)
    assert str(err.value) == "store is at schema version 1, older than the 5 this code expects"


def test_connect_readonly_refuses_an_empty_database(tmp_path):
    path = tmp_path / "cox.db"
    sqlite3.connect(path).close()
    with pytest.raises(read.StoreVersionError, match="schema version 0, older"):
        read.connect_readonly(f"sqlite:///{path}")


def test_connect_readonly_refuses_a_missing_file_and_does_not_create_it(tmp_path):
    path = tmp_path / "typo.db"
    with pytest.raises(FileNotFoundError) as err:
        read.connect_readonly(f"sqlite:///{path}")
    assert str(err.value) == f"no store at {path}: connect_readonly never creates one"
    assert list(tmp_path.iterdir()) == []


SIDECARS = {"cox.db-shm", "cox.db-wal"}


def wal_store(directory):
    """A store made the way production makes one: open_store, so journal_mode is WAL."""
    path = directory / "cox.db"
    open_store(f"sqlite:///{path}", NOW).close()
    raw = sqlite3.connect(path)
    assert raw.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    raw.close()  # the last connection to close removes the sidecars
    return path


def names(directory):
    return {p.name for p in directory.iterdir()}


def test_reading_a_wal_store_leaves_the_file_bytes_unchanged_and_refuses_a_write(tmp_path):
    path = wal_store(tmp_path)
    before = path.read_bytes()
    conn = read.connect_readonly(f"sqlite:///{path}")
    read.list_runs(conn, "", 5)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("INSERT INTO leases (name) VALUES ('x')")
    conn.close()
    assert path.read_bytes() == before
    assert names(tmp_path) - {"cox.db"} <= SIDECARS  # the -shm and -wal a WAL read needs may be left behind


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write to any directory")
def test_a_read_only_directory_with_no_wal_reads_immutable_and_creates_nothing(tmp_path):
    path = wal_store(tmp_path)
    assert names(tmp_path) == {"cox.db"}
    before = path.read_bytes()
    tmp_path.chmod(0o555)
    try:
        assert "immutable=1" in read._sqlite_uri(path)
        conn = read.connect_readonly(f"sqlite:///{path}")
        assert read.list_runs(conn, "", 5) == []
        conn.close()
        assert names(tmp_path) == {"cox.db"}
        assert path.read_bytes() == before
    finally:
        tmp_path.chmod(0o755)


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write to any directory")
def test_a_read_only_directory_with_a_live_writer_reads_its_uncheckpointed_rows(tmp_path):
    path = wal_store(tmp_path)
    live = sqlite3.connect(path)
    live.execute("INSERT INTO runs (run_id, started_at) VALUES ('live', '2026-09-24T09:00:00Z')")
    live.commit()
    assert names(tmp_path) == {"cox.db"} | SIDECARS
    tmp_path.chmod(0o555)
    try:
        assert "immutable" not in read._sqlite_uri(path)
        conn = read.connect_readonly(f"sqlite:///{path}")
        assert [r["run_id"] for r in read.list_runs(conn, "", 5)] == ["live"]
        conn.close()
    finally:
        tmp_path.chmod(0o755)
        live.close()


@pytest.mark.parametrize("url", ["sqlite://", "sqlite:///:memory:", "sqlite:////abs/cox.db", "sqlite:///rel/cox.db"])
def test_the_url_parser_agrees_with_store_dialects(url):
    target = _sqlite_target(url)
    assert read._sqlite_file(url) == (None if target == ":memory:" else target)


def test_the_url_parser_refuses_what_store_dialect_refuses_with_the_same_message():
    with pytest.raises(ValueError) as theirs:
        _sqlite_target("sqlite://cox.db")
    with pytest.raises(ValueError) as ours:
        read._sqlite_file("sqlite://cox.db")
    assert str(ours.value) == str(theirs.value)
    assert read._sqlite_file("postgresql://h/db") is None


class FakeRaw:
    """A DB-API connection that records every statement and answers by matching its text."""

    def __init__(self):
        self.sent: list[tuple[str, tuple]] = []
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def rollback(self):
        pass

    def close(self):
        self.closed = True


class FakeCursor:
    rowcount = 0

    def __init__(self, raw):
        self.raw = raw
        self.rows: list[tuple] = []

    def execute(self, sql, params=()):
        self.raw.sent.append((sql, tuple(params)))
        replies = (
            ("information_schema", [(1,)]),
            ("MAX(version)", [(5,)]),
            ("FROM graphs", [("g1", "review", "1", "g1", "t", "{}")]),
            ("LEFT JOIN", [(1, 0.5, 1, 10, 5, 3)]),
        )
        self.rows = next((rows for text, rows in replies if text in sql), [])

    def fetchall(self):
        return self.rows


def test_connect_readonly_on_postgres_sets_the_session_read_only_before_any_query(monkeypatch):
    fake = FakeRaw()
    monkeypatch.setattr(read, "connect", lambda url: Connection(fake, POSTGRES))
    conn = read.connect_readonly("postgresql://host/db")
    assert fake.sent[0] == ("SET default_transaction_read_only = on", ())
    assert len(fake.sent) == 3 and conn.dialect is POSTGRES


def test_connect_readonly_closes_a_postgres_connection_that_is_at_the_wrong_version(monkeypatch):
    fake = FakeRaw()
    monkeypatch.setattr(read, "connect", lambda url: Connection(fake, POSTGRES))
    monkeypatch.setattr(read, "check_version", lambda conn: (1, 2))
    with pytest.raises(read.StoreVersionError):
        read.connect_readonly("postgresql://host/db")
    assert fake.closed


# SQLite only: a file path opened read-only.
def test_connect_readonly_refuses_a_newer_database(tmp_path):
    url = f"sqlite:///{tmp_path / 'cox.db'}"
    c = open_store(url, NOW)
    put(c, "schema_version", version=6, applied_at=NOW, description="future")
    c.close()
    with pytest.raises(read.StoreVersionError) as err:
        read.connect_readonly(url)
    assert str(err.value) == "store is at schema version 6, newer than the 5 this code expects"


READERS = (
    lambda c: read.list_runs(c, "", 5),
    lambda c: read.run_summary(c, "r1"),
    lambda c: read.cost_by_model(c, "r1"),
    lambda c: read.calls(c, "r1"),
    lambda c: read.calls(c, "r1", "build"),
    lambda c: read.attempts(c, "t1"),
    lambda c: read.ledger_rows(c, "r1"),
    lambda c: read.gate_decisions(c, "r1"),
    lambda c: read.current_lease(c, "epic"),
    lambda c: read.run_graph(c, "r1"),
)


def sent_by_each_reader(conn, monkeypatch):
    """(sql, params) pairs each reader sends, one list per reader, seen at Connection.query_all."""
    sent: list[tuple[str, tuple]] = []
    real = Connection.query_all

    def spy(self, sql, params=()):
        sent.append((sql, tuple(params)))
        return real(self, sql, params)

    monkeypatch.setattr(Connection, "query_all", spy)
    per_reader = []
    for reader in READERS:
        start = len(sent)
        reader(conn)
        per_reader.append(sent[start:])
    return per_reader


def test_every_reader_sends_portable_queries_with_one_sqlite_placeholder_per_parameter(sqlite_conn, monkeypatch):
    # SQLite only: it asserts the `?` placeholder, which Postgres never sends.
    per_reader = sent_by_each_reader(sqlite_conn, monkeypatch)
    assert all(queries for queries in per_reader)
    for sql, params in (q for queries in per_reader for q in queries):
        assert forbidden_constructs(sql) == ()
        assert sql.count("?") == len(params) and "%s" not in sql


def test_every_reader_sends_portable_queries_with_one_postgres_placeholder_per_parameter(monkeypatch):
    fake = FakeRaw()
    conn = Connection(fake, POSTGRES)
    for reader in READERS:
        before = len(fake.sent)
        reader(conn)
        assert len(fake.sent) > before
    assert any("FROM graph_nodes" in s for s, _ in fake.sent) and any("FROM graph_edges" in s for s, _ in fake.sent)
    for sql, params in fake.sent:
        assert forbidden_constructs(sql) == ()
        assert sql.count("%s") == len(params) and "?" not in sql


def _third_party_roots_with_the_harness_package_stubbed():
    """Import harness.store_read in a fresh interpreter whose `harness` is an empty package.

    The real harness/__init__.py imports core, graphs and yaml, which hides what store_read
    itself pulls. A stub with the same __path__ lets the submodules load without it.
    """
    code = (
        "import sys, types\n"
        "pkg = types.ModuleType('harness')\n"
        f"pkg.__path__ = [{str(Path(read.__file__).parent)!r}]\n"
        "sys.modules['harness'] = pkg\n"
        "import harness.store_read\n"
        "print(sorted({m.split('.')[0] for m in sys.modules if m.split('.')[0] not in sys.stdlib_module_names}))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    # Interpreter and virtualenv machinery (__main__, _virtualenv, editable finders, cython_runtime) is not an import.
    return {n for n in ast.literal_eval(out) if not n.startswith("_") and n != "cython_runtime"}


def test_store_read_and_what_it_imports_pull_no_module_outside_harness_and_runner():
    assert _third_party_roots_with_the_harness_package_stubbed() - {"harness", "runner"} == set()


def _offending_imports(source: str) -> list[str]:
    """Imports anywhere in the source, function bodies included, that the ticket does not allow."""
    tree = ast.parse(source)
    pairs = [(a.name, "") for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names] + [
        (n.module or "", a.name) for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names
    ]
    allowed = lambda module, name: (  # noqa: E731
        module.split(".")[0] in sys.stdlib_module_names
        or module == "harness.store_dialect"
        or (module == "harness.store_migrate" and name == "check_version")
        or (module == "harness" and name == "store_traces")
    )
    dynamic = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
    }
    return [f"{m}.{n}".strip(".") for m, n in pairs if not allowed(m, n)] + sorted(dynamic & {"__import__", "import_module"})


def test_store_read_imports_the_stdlib_store_dialect_store_traces_and_check_version_only():
    assert _offending_imports(Path(read.__file__).read_text()) == []


def test_the_import_check_sees_lazy_imports_and_a_bare_harness_import():
    lazy = "def f():\n    import graphs\n"
    assert _offending_imports(lazy) == ["graphs"]
    assert _offending_imports("from harness import cos\n") == ["harness.cos"]
    assert _offending_imports("import importlib\nimportlib.import_module('yaml')\n") == ["import_module"]


def test_read_trace_of_an_unknown_call_is_empty(tmp_path):
    assert read.read_trace(tmp_path, "r1", "c1") == []


@pytest.mark.xfail(
    strict=True,
    reason="finding: harness/__init__.py imports core, graphs and (via core) yaml, so any harness.* import "
    "pulls them. The ticket forbids editing __init__.py. Remove this mark once the package import is light.",
)
def test_importing_store_read_pulls_only_stdlib_harness_and_runner_modules():
    code = (
        "import sys; import harness.store_read; "
        "print(sorted({m.split('.')[0] for m in sys.modules if m.split('.')[0] not in sys.stdlib_module_names}))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    names = set(ast.literal_eval(out))
    # Interpreter and virtualenv machinery (__main__, _virtualenv, editable finders, cython_runtime) is not an import.
    imported = {n for n in names if not n.startswith("_") and n != "cython_runtime"}
    assert imported - {"harness", "runner"} == set()
