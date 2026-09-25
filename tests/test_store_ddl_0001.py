import re

import pytest

import harness.store_ddl_0001 as ddl
from harness.store_dialect import POSTGRES, SQLITE, connect, forbidden_constructs
from harness.store_migrate import check_version, migrate

NOW = "2026-09-24T00:00:00Z"

PKS = {
    "runs": ["run_id"],
    "phases": ["run_id", "phase_id"],
    "tasks": ["run_id", "task_id"],
    "attempts": ["run_id", "task_id", "seq"],
    "node_calls": ["call_id"],
    "ledger": ["row_hash"],
    "gate_decisions": ["run_id", "phase_id", "seq"],
    "leases": ["name"],
}

NODE_CALLS = [
    "call_id",
    "run_id",
    "seq",
    "phase_id",
    "task_id",
    "role",
    "tier",
    "model_alias",
    "model_id",
    "claude_code_version",
    "cost_usd",
    "ceiling_usd",
    "ceiling_source",
    "turns",
    "duration_ms",
    "input_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "input_total",
    "output_tokens",
    "ok",
    "ts",
    "requested_tier",
    "chosen_tier",
    "decision_reason",
    "router_tier",
    "router_reason",
    "ticket_key",
    "outcome_key",
    "system_one_prediction",
    "system_one_confidence",
    "decision_json",
    "detail_json",
]
LEDGER = [
    "row_hash",
    "run_id",
    "ts",
    "principal",
    "kind",
    "risk",
    "outcome",
    "cartridge_sha",
    "provider_profile",
    "schema_tag",
    "epoch",
    "row_json",
]
GATE = [
    "run_id",
    "phase_id",
    "seq",
    "kind",
    "target",
    "decision",
    "risk",
    "outcome",
    "applied",
    "edited",
    "epoch",
    "detail_json",
]
JSON_COLUMNS = ["decision_json", "detail_json", "detail_json", "record_json", "record_json", "row_json", "totals_json"]


@pytest.fixture
def conn():
    c = connect("sqlite:///:memory:")
    migrate(c, NOW, [ddl])
    yield c
    c.close()


def info(conn, table):
    return conn.query_all(f"PRAGMA table_info({table})")


def test_open_store_leaves_schema_version_at_one(conn):
    assert conn.query_one("SELECT MAX(version) FROM schema_version") == (1,)
    assert check_version(conn, [ddl]) == (1, 1)


def test_migrating_again_applies_nothing_new(conn):
    assert migrate(conn, NOW, [ddl]) == 1
    assert conn.query_one("SELECT COUNT(*) FROM schema_version") == (1,)


@pytest.mark.parametrize("table", sorted(PKS))
def test_each_table_exists_with_the_listed_primary_key(conn, table):
    rows = info(conn, table)
    assert rows
    assert [r[1] for r in sorted((r for r in rows if r[5]), key=lambda r: r[5])] == PKS[table]


@pytest.mark.parametrize(("table", "names"), [("node_calls", NODE_CALLS), ("ledger", LEDGER), ("gate_decisions", GATE)])
def test_column_names_are_the_contract(conn, table, names):
    assert [r[1] for r in info(conn, table)] == names


@pytest.mark.parametrize("table", ["ledger", "gate_decisions"])
def test_epoch_is_a_nullable_bigint(conn, table):
    epoch = next(r for r in info(conn, table) if r[1] == "epoch")
    assert (epoch[2], epoch[3]) == ("BIGINT", 0)


def test_the_four_indexes_exist(conn):
    rows = conn.query_all("SELECT tbl_name FROM sqlite_master WHERE type = 'index' AND name LIKE 'ix_%'")
    assert sorted(t for (t,) in rows) == ["attempts", "ledger", "node_calls", "node_calls"]


@pytest.mark.parametrize("dialect", [SQLITE, POSTGRES], ids=["sqlite", "postgres"])
def test_every_statement_is_portable(dialect):
    sql = ddl.statements(dialect)
    assert len(sql) == 12
    assert [forbidden_constructs(s) for s in sql] == [()] * len(sql)


@pytest.mark.parametrize(("dialect", "want"), [(SQLITE, "TEXT"), (POSTGRES, "JSONB")], ids=["sqlite", "postgres"])
def test_json_columns_use_the_dialect_json_type(dialect, want):
    found = [m for s in ddl.statements(dialect) for m in re.findall(r"^\s+(\w+_json) (\w+)", s, re.MULTILINE)]
    assert sorted(c for c, _ in found) == JSON_COLUMNS
    assert {t for _, t in found} == {want}


def test_postgres_booleans_are_smallint():
    text = "\n".join(ddl.statements(POSTGRES))
    assert re.search(r"\bok SMALLINT\b", text)
    assert not re.search(r"BOOLEAN|SERIAL|TIMESTAMP", text)
