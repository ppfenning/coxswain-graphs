# Storage

This page describes where run data lives, which repository owns the schema, and how a chair moves historical records into the database by hand. Every table and column below is read from `harness/store_ddl_0001.py` and `harness/store_ddl_0002.py`. A column those modules do not define is not documented here.

## The split

Three kinds of data live in three places.

Work items and intake stay markdown in git. Nothing in this page changes that.

Run records live in the database. That covers runs, phases, tasks, attempts, model calls, the ledger, gate decisions and the leader lease, plus the graphs registry.

Traces live in day-partitioned compressed files outside git. The layout is `YYYY/MM/DD/<run_id>.jsonl.zst` under a trace root, as the docstring of `harness/store_traces.py` states. One file holds one run for one day. Each append is one zstd frame.

## Who owns what

The graphs repository owns the DDL, the numbered migrations, the `schema_version` table, the graphs registry and the dialect shim. The shim is `harness/store_dialect.py`. The migrations and their runner are in `harness/store_migrate.py` and the `harness/store_ddl_NNNN.py` modules.

The profile carries one thing: the connection string, under the key `storage_url`. `_storage_url` in `harness/cli.py` returns it, and falls back to the sqlite file inside the runs directory when the key is absent. The accepted schemes are `sqlite://` and `postgresql://`.

The tools repository never applies migrations. It reads through `harness/store_read.py`, or with plain SQL over the tables documented below. `connect_readonly` in that module connects without writing and calls `check_version`. If the stored version differs from the version the code expects, it closes the connection and raises `StoreVersionError`. Only `open_store` in `harness/store_migrate.py` applies migrations.

The read functions in `harness/store_read.py` are `list_runs`, `run_summary`, `cost_by_model`, `calls`, `attempts`, `ledger_rows`, `gate_decisions`, `current_lease`, `run_graph` and `read_trace`. All but `read_trace` take a connection. `read_trace` takes a trace root, a run id and a call id.

## Table reference

Timestamps are ISO-8601 TEXT. Booleans are SMALLINT holding 0 or 1. JSON columns are TEXT on sqlite and JSONB on Postgres, and `connect` hands both back as text. Every table has a primary key.

### schema_version

Created by `harness/store_migrate.py`, not by a DDL module. Columns: `version` INTEGER, the primary key; `applied_at` TEXT; `description` TEXT.

### runs

Primary key `run_id`. TEXT columns: `run_id`, `principal`, `launched_by`, `launched_at`, `cartridge_sha`, `cartridge_team`, `overlay_sha`, `provider_profile`, `started_at`, `ended_at`, `status`. JSON column: `record_json`. Migration 0002 adds `graph_id`, a nullable TEXT column.

### phases

Primary key `run_id`, `phase_id`. TEXT columns: `run_id`, `phase_id`, `ts`, `principal`. REAL column: `human_minutes`. JSON columns: `totals_json`, `record_json`.

### tasks

Primary key `run_id`, `task_id`. TEXT columns: `run_id`, `phase_id`, `task_id`, `state`, `updated_at`.

### attempts

Primary key `run_id`, `task_id`, `seq`. TEXT columns: `run_id`, `task_id`, `phase_id`, `kind`, `reason`, `ts`. INTEGER column: `seq`. Index `ix_attempts_task` on `task_id`.

### node_calls

Primary key `call_id`.

- TEXT: `call_id`, `run_id`, `phase_id`, `task_id`, `role`, `tier`, `model_alias`, `model_id`, `claude_code_version`, `ceiling_source`, `ts`, `requested_tier`, `chosen_tier`, `decision_reason`, `router_tier`, `router_reason`, `ticket_key`, `outcome_key`, `system_one_prediction`.
- INTEGER: `seq`, `turns`, `duration_ms`, `input_tokens`, `cache_read_tokens`, `cache_creation_tokens`, `input_total`, `output_tokens`.
- REAL: `cost_usd`, `ceiling_usd`, `system_one_confidence`.
- Boolean: `ok`.
- JSON: `decision_json`, `detail_json`.
- Added by migration 0002: `node_id`, a nullable TEXT column.

Indexes: `ix_node_calls_run` on `run_id`, and `ix_node_calls_role_tier` on `role`, `tier`.

### ledger

Primary key `row_hash`. TEXT columns: `row_hash`, `run_id`, `ts`, `principal`, `kind`, `risk`, `outcome`, `cartridge_sha`, `provider_profile`, `schema_tag`. BIGINT column: `epoch`. JSON column: `row_json`. Index `ix_ledger_run` on `run_id`.

### gate_decisions

Primary key `run_id`, `phase_id`, `seq`. TEXT columns: `run_id`, `phase_id`, `kind`, `target`, `decision`, `risk`, `outcome`. INTEGER column: `seq`. Boolean columns: `applied`, `edited`. BIGINT column: `epoch`. JSON column: `detail_json`.

### leases

Primary key `name`. TEXT columns: `name`, `holder`, `heartbeat_at`, `expires_at`. BIGINT column: `epoch`.

### graphs

Added by migration 0002. Primary key `graph_id`. TEXT columns: `graph_id`, `name`, `version`, `content_hash`, `registered_at`. JSON column: `definition_json`. Index `ix_graphs_name_version` on `name`, `version`. The `graph_id` equals the `content_hash`. `version` is TEXT, so the registration task must write it as text.

### graph_nodes

Added by migration 0002. Primary key `graph_id`, `node_id`. TEXT columns: `graph_id`, `node_id`, `role`, `default_tier`, `default_class`, `output_schema_hash`. INTEGER column: `ord`.

### graph_edges

Added by migration 0002. Primary key `graph_id`, `src`, `dst`. All three are TEXT.

### Joins are by value

`runs.graph_id` and `node_calls.node_id` carry no foreign-key clause, because SQLite cannot add a constraint by ALTER TABLE. The join to `graphs` and `graph_nodes` is by value, and nothing in the database enforces it. Existing rows keep NULL in both columns.

## Adding a migration

A migration is a module in the `harness` package named `store_ddl_NNNN`, four digits, contiguous from 0001. It exposes `VERSION`, `DESCRIPTION` and `statements(dialect)`. The number in the name and `VERSION` must agree, or the runner raises `MigrationError`. A gap or a duplicate number raises the same error.

Adding one never edits `harness/store_migrate.py`, because `default_modules` finds the modules with `pkgutil`. To add migration 0003, create `harness/store_ddl_0003.py` with `VERSION = 3`.

Test it two ways. From an empty database, it must reach the new version. From a database at the previous version, it must apply only the new migration. `tests/test_store_ddl_0002.py` does both and is the pattern to copy: see `test_an_empty_database_reaches_version_two` and `test_a_database_at_version_one_applies_only_0002`.

## Portability rules

The store runs on sqlite and on Postgres, so the SQL in it stays portable.

- Timestamps are ISO-8601 TEXT. Booleans are SMALLINT 0 or 1. JSON columns use `dialect.json_type`.
- Every table has a primary key, so a writer can use `insert_ignore` or `upsert` from `harness/store_dialect.py`.
- SQL takes its placeholder from `dialect.placeholder`, which is `?` on sqlite and `%s` on Postgres.
- A column added by ALTER TABLE carries no foreign-key clause, because SQLite cannot add a constraint that way. The docstring of `harness/store_ddl_0002.py` explains.

`forbidden_constructs` in `harness/store_dialect.py` returns the banned tokens found in a piece of SQL. The banned tokens are `AUTOINCREMENT`, `PRAGMA`, `INSERT OR REPLACE` (which also catches `REPLACE INTO`), `strftime` and `json_extract`. The last two count only as calls, so a column named `strftime` passes. Comments and quoted text are blanked before matching.

## The lease and the fencing epoch

`harness/store_lease.py` keeps a leader lease as one row per `name` in `leases`, fenced by a monotonic `epoch`. Time is always an argument: an ISO timestamp string and a ttl in seconds. Nothing in the module reads the clock. Every state change is one conditional statement whose affected row count decides the outcome, so two writers cannot both win.

- `acquire(conn, name, holder, now, ttl)` takes a free or expired lease and increments `epoch`. The current holder acquiring again also increments it. A refusal is a `LeaseResult` with `ok` False and the current holder.
- `renew(conn, name, holder, epoch, now, ttl)` extends a live lease. An expired or released lease is not revived, and the holder must acquire again.
- `release(conn, name, holder, epoch)` expires the lease in the past. The row and its epoch stay, so the next holder still increments.
- `assert_epoch(conn, name, epoch, now)` is the fence. It is true only when `epoch` is the stored one and the lease is unexpired at `now`.
- `lease_state(row, now)` returns `free`, `expired` or `held`.

Every leader write calls `assert_epoch` immediately before it writes, and carries the epoch. The `ledger` and `gate_decisions` tables each have an `epoch` column.

## Querying traces and joining to the database

A trace row is one JSON object per line with the keys `run_id`, `call_id`, `seq` and `event`, as `to_lines` in `harness/store_traces.py` writes it. The `call_id` is the same value as `node_calls.call_id`.

The DuckDB query below reads the whole trace tree with one glob and joins each row to its call. It needs DuckDB's sqlite extension. The paths are placeholders for your trace root and your database file. The query has not been run. The layout and the `call_id` key come from `harness/store_traces.py`. The `compression` and `format` options of `read_json` come from DuckDB's own documentation and were not tried against a file written by `append_call`. Run it once on your DuckDB version before relying on it.

```sql
INSTALL sqlite;
LOAD sqlite;
ATTACH '/data/cox.db' AS cox (TYPE sqlite, READ_ONLY);

SELECT c.role, c.tier, c.cost_usd, count(*) AS events
FROM read_json('/data/traces/*/*/*/*.jsonl.zst', compression = 'zstd', format = 'newline_delimited') AS t
JOIN cox.node_calls AS c ON c.call_id = t.call_id
GROUP BY c.role, c.tier, c.cost_usd
ORDER BY events DESC;
```

The same read from Python is `read_trace(root, run_id, call_id)` in `harness/store_read.py`, which calls `read_call` in `harness/store_traces.py`. Reading traces from Python needs the `traces` extra in `pyproject.toml`.

## Postgres, the next step

Stay on sqlite while one machine writes the run records. Move to Postgres only when records are written from more than one machine. The tables are the same. The dialect shim renders the JSON type as JSONB and the placeholder as `%s`.

A `postgresql://` URL needs the `postgres` extra in `pyproject.toml`. Without the driver, `connect` raises `StoreDriverMissing`.

A live Postgres run of the migrations is a chair step. The tests cover only rendering. The comment on `_has_version_table` in `harness/store_migrate.py` reads "unknown: the postgres branch is not run in this repository's tests". The docstring of `harness/store_read.py` says the read-only session statement in `connect_readonly` has not been run against a server. Run the migrations against a real server, and check the result, before pointing anything at it.

## Chair runbook: backfill by hand

This moves historical files into the store. Run the two commands in this order. Every path is an argument.

### 1. Run records

```
python -m harness.store_backfill RUNS_DIR WORK_DIR LEDGER STORE_URL
```

- `RUNS_DIR` is the directory of run, phase, launch, call and usage files.
- `WORK_DIR` is the directory of task markdown files.
- `LEDGER` is the ledger file, one JSON object per line.
- `STORE_URL` is `sqlite:///<absolute path>` or `postgresql://...`. The command calls `open_store`, so it applies migrations.

It prints a JSON report and exits 0 only when every table balances. The report has one entry per table: `runs`, `phases`, `gate_decisions`, `node_calls`, `ledger` and `attempts`. Each entry has four counts: `seen`, `inserted`, `already_present` and `malformed`. A table balances when `seen` minus `malformed` equals `inserted` plus `already_present`. Exit 1 means at least one table does not balance.

### 2. Traces

```
python -m harness.store_backfill_traces LOOSE_ROOT CALLS_DIR NEW_ROOT [--archive DIR]
```

- `LOOSE_ROOT` is the directory of `<run_id>-trace` directories.
- `CALLS_DIR` is the directory of `<run_id>.calls.jsonl` files, which give each call its day and call id.
- `NEW_ROOT` is the trace store root to write.
- `--archive DIR` is optional. It moves each source into `DIR` after the source reads back from the store with the same event count. It never deletes.

It prints one `name: value` line per field: `seen`, `appended`, `present`, `unmatched`, `archived`, `src_bytes` and `dst_bytes`. It exits 0 only when `seen` equals `appended` plus `present`. An empty trace file counts as seen and stays unimported, which fails the run. A trace with no matching call is imported as `<run_id>-<role>-<n>`, dated by file modification time, and counted in `unmatched`.

### The archive rule

Files are archived only after the counts balance and match the stats database. Balanced counts show that the importer wrote what it read. They do not show that it read everything, so compare the per-table counts in the reports against the stats database before archiving anything.

`archive_imported` in `harness/store_backfill.py` refuses and moves nothing unless the report balances, and it never deletes. It is a function, not a flag of the command. The comparison with the stats database is a chair step, and neither module does it.

`--archive` on the trace command acts in the same run that imports. To keep the rule, run the trace command first without `--archive`, compare the counts, then run it again with `--archive`. A file already in the store counts as `present`, and the second run archives it.

### Cutover

The cutover phase is launched only after tools reads the database.
