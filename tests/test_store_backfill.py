# Answers to the plan's three unknowns:
#   frontmatter loader: core.workstore.read_item, the one tests/test_epic_driver.py and the driver use.
#   store from a URL: harness.store_migrate.open_store(url, now); an absolute sqlite path is sqlite:////abs.
#   launch record name: not fixed by the ticket; the importer takes <run>.launch.json or <run>.json, told apart by content.
import json
import os

import pytest

from harness.store_backfill import (
    TABLES,
    ArchiveRefused,
    _ended_at,
    _mtime_iso,
    archive_imported,
    backfill,
    balanced,
    main,
    parse_attempts,
    parse_call_lines,
    parse_ledger_line,
    parse_usage,
    run_files,
)
from harness.store_dialect import default_url, json_load
from harness.store_migrate import open_store
from harness.store_write import Store

NOW = "2026-09-24T00:00:00Z"
TS = "2026-09-24T13:58:06.378339+00:00"
GATE = {
    "applied": True,
    "decision": "approved",
    "edited": False,
    "kind": "item_create",
    "outcome": "clean",
    "risk": "low",
    "target": "T-1",
}
RECORD = {
    "cartridge_sha": "bd20",
    "cartridge_team": "pat",
    "gate_diffs": [],
    "human_minutes": 0.0,
    "overlay_sha": None,
    "principal": "epic-swarm",
    "proposals": [],
    "provider_profile": "claude-code@f9",
    "totals": {"completed": 1},
    "ts": TS,
}


def call(call_id, **extra):
    return {
        "role": "plan",
        "task_id": None,
        "tier": "standard",
        "model": "sonnet",
        "cost_usd": 0.1,
        "ceiling_usd": 1.0,
        "ceiling_source": "profile",
        "turns": 4,
        "duration_ms": 10,
        "input_tokens": 4,
        "cache_read_tokens": 1,
        "cache_creation_tokens": 2,
        "input_total": 7,
        "output_tokens": 9,
        "id": call_id,
        "ts": TS,
        "ok": True,
        **extra,
    }


def ledger_line(run_id, kind="item_create", **extra):
    entry = {
        "run_id": run_id,
        "ts": TS,
        "principal": "epic-swarm",
        "kind": kind,
        "risk": "low",
        "outcome": "clean",
        "cartridge_sha": "bd20",
        "provider_profile": "claude-code@f9",
        **extra,
    }
    return json.dumps(entry)


def task_file(task_id, attempts):
    lines = [f"  - {{run: {r}, phase: {p}, reason: '{why}', ts: '{TS}', kind: {k}}}" for r, p, why, k in attempts]
    return (
        f"---\nid: {task_id}\nphase: p1\nstate: ready\nneeds: []\nsurfaces: []\ntitle: {task_id}\nattempts:\n"
        + "\n".join(lines)
        + "\n---\n\nbody\n"
    )


@pytest.fixture
def tree(tmp_path):
    """Two runs, two phases, one launch, a usage-only run, a ledger of three rows and two task files."""
    runs, work = tmp_path / "runs", tmp_path / "work" / "p1"
    runs.mkdir()
    work.mkdir(parents=True)

    def put(name, doc):
        (runs / name).write_text(json.dumps(doc))

    put("run-a.json", {**RECORD, "run_id": "run-a", "gate_diffs": [GATE, GATE]})
    put("run-a.launch.json", {"launched_by": "chair-2026-09-23", "at": TS})
    put("run-a:p1.json", {**RECORD, "run_id": "run-a:p1", "gate_diffs": [GATE]})
    (runs / "run-a.calls.jsonl").write_text("\n".join(json.dumps(call(f"a{i}")) for i in (1, 2)) + "\n")
    put("run-a.usage.json", {"run_id": "run-a", "summary": {"calls": 2}, "calls": [call("a1"), call("a2")]})
    put("run-b.json", {**RECORD, "run_id": "run-b"})
    put("run-b:p1.json", {**RECORD, "run_id": "run-b:p1", "gate_diffs": [GATE]})
    put("run-b.usage.json", {"run_id": "run-b", "summary": {"calls": 3}, "calls": [call(f"b{i}") for i in (1, 2, 3)]})
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("\n".join(ledger_line(r, k) for r, k in (("run-a", "x"), ("run-a", "y"), ("run-b", "x"))) + "\n")
    (tmp_path / "work" / "initiative.md").write_text("---\nid: demo\ntitle: demo\n---\n\ngoal\n")
    (work / "t1.md").write_text(
        task_file("t1", [("run-a", "p1", "first", "refused"), ("run-a", "p1", "second", "quarantined")])
    )
    (work / "t2.md").write_text(task_file("t2", [("run-b", "p1", "only", "refused")]))
    return tmp_path


@pytest.fixture
def store():
    conn = open_store("sqlite:///:memory:", NOW)
    yield Store(conn)
    conn.close()


def run(store, tree):
    return backfill(store, tree / "runs", tree / "work", tree / "ledger.jsonl")


def counts(report, table):
    return tuple(report[table][k] for k in ("seen", "inserted", "already_present", "malformed"))


# ── pure parsers ─────────────────────────────────────────────────────────────


def test_a_call_line_that_is_not_json_is_counted_and_extra_keys_go_to_detail():
    lines = [json.dumps(call("c1", tools=["Read"])), "not json", "", "[1]", json.dumps({"role": "plan"})]
    rows, bad = parse_call_lines("r", lines)
    assert [(r["call_id"], r["seq"], r["run_id"]) for r in rows] == [("c1", 0, "r"), ("legacy:r:3", 3, "r")]
    assert rows[0]["detail_json"] == {"tools": ["Read"]}
    assert bad == 2


def test_usage_calls_take_the_shape_of_call_lines_and_a_usage_without_a_list_is_one_malformed_record():
    calls = [call("c1"), call("c2")]
    assert parse_usage("r", {"calls": calls}) == parse_call_lines("r", [json.dumps(c) for c in calls])
    assert parse_usage("r", {"summary": {}}) == ([], 1)
    assert parse_usage("r", None) == ([], 1)


def test_ledger_rows_keep_the_source_entry_and_reject_a_row_without_a_run_id():
    row = parse_ledger_line(ledger_line("run-a", schema=3))
    assert row["run_id"] == "run-a" and row["row_json"]["schema"] == 3
    assert parse_ledger_line(json.dumps({"ts": TS, "kind": "x"})) is None
    assert parse_ledger_line("{oops") is None


def test_attempt_seq_counts_earlier_attempts_of_the_same_run_only():
    entry = {"run": "r1", "phase": "p1", "reason": "why", "ts": TS, "kind": "refused"}
    rows, bad = parse_attempts("t1", [entry, {**entry, "run": "r2"}, entry, {"run": "r1"}])
    assert [(r["run_id"], r["seq"]) for r in rows] == [("r1", 0), ("r2", 0), ("r1", 1)]
    assert bad == 1
    assert parse_attempts("t1", None) == ([], 0)
    assert parse_attempts("t1", "nope") == ([], 1)


def test_the_ended_at_is_the_latest_call_ts_else_the_file_mtime_in_utc():
    assert _mtime_iso(0.0) == "1970-01-01T00:00:00+00:00"
    assert _ended_at("2026-09-24T13:58:06+00:00", 0.0) == "2026-09-24T13:58:06+00:00"
    assert _ended_at(None, 0.0) == "1970-01-01T00:00:00+00:00"


def test_balanced_needs_every_table_to_add_up():
    ok = {"runs": {"seen": 3, "inserted": 1, "already_present": 1, "malformed": 1}}
    assert balanced(ok)
    assert balanced({**ok, "runs_stamped_ended": 4})
    assert not balanced({"runs_stamped_ended": 4})
    assert not balanced({**ok, "phases": {"seen": 2, "inserted": 1, "already_present": 0, "malformed": 0}})
    assert not balanced({})


# ── the edge ─────────────────────────────────────────────────────────────────


def test_a_fixture_tree_imports_to_the_expected_row_counts(store, tree):
    report = run(store, tree)
    assert counts(report, "runs") == (2, 2, 0, 0)
    assert counts(report, "phases") == (2, 2, 0, 0)
    assert counts(report, "gate_decisions") == (4, 4, 0, 0)
    assert counts(report, "node_calls") == (5, 5, 0, 0)
    assert counts(report, "ledger") == (3, 3, 0, 0)
    assert counts(report, "attempts") == (3, 3, 0, 0)
    assert balanced(report)
    assert {t: store.total_rows(t) for t in TABLES} == {
        "runs": 2,
        "phases": 2,
        "gate_decisions": 4,
        "node_calls": 5,
        "ledger": 3,
        "attempts": 3,
    }


def test_the_launch_record_lands_on_its_run_and_a_run_with_call_lines_and_usage_imports_calls_once(store, tree):
    run(store, tree)
    launched = store.conn.query_all("SELECT run_id, launched_by, launched_at FROM runs ORDER BY run_id")
    assert launched == [("run-a", "chair-2026-09-23", TS), ("run-b", None, None)]
    per_run = store.conn.query_all("SELECT run_id, COUNT(*) FROM node_calls GROUP BY run_id ORDER BY run_id")
    assert per_run == [("run-a", 2), ("run-b", 3)]
    assert (
        json_load(store.conn.query_one("SELECT record_json FROM phases WHERE run_id = 'run-a'")[0])["run_id"]
        == "run-a:p1"
    )


def test_a_second_run_inserts_nothing_and_reports_every_row_as_present(store, tree):
    run(store, tree)
    again = run(store, tree)
    assert [again[t]["inserted"] for t in TABLES] == [0] * 6
    assert counts(again, "node_calls") == (5, 0, 5, 0)
    assert counts(again, "attempts") == (3, 0, 3, 0)
    assert balanced(again)
    assert store.total_rows("node_calls") == 5


def test_malformed_lines_are_counted_and_skipped_not_fatal(store, tree):
    with (tree / "runs" / "run-a.calls.jsonl").open("a") as f:
        f.write("not json\n")
    with (tree / "ledger.jsonl").open("a") as f:
        f.write("[1, 2]\n")
    (tree / "runs" / "broken.json").write_text("{")
    report = run(store, tree)
    assert counts(report, "node_calls") == (6, 5, 0, 1)
    assert counts(report, "ledger") == (4, 3, 0, 1)
    assert counts(report, "runs") == (3, 2, 0, 1)
    assert balanced(report)


def test_a_launch_with_no_run_record_and_no_phase_builds_a_run_from_the_launch_alone(store, tree):
    (tree / "runs" / "run-z.launch.json").write_text(json.dumps({"launched_by": "chair", "at": TS}))
    assert counts(run(store, tree), "runs") == (3, 3, 0, 0)
    row = store.conn.query_one("SELECT launched_by, principal FROM runs WHERE run_id = 'run-z'")
    assert tuple(row) == ("chair", None)


def test_a_launched_json_launch_pairs_with_its_run(store, tree):
    (tree / "runs" / "run-a.launch.json").unlink()
    (tree / "runs" / "run-a.launched.json").write_text(json.dumps({"launched_by": "chair-x", "at": TS}))
    report = run(store, tree)
    row = store.conn.query_one("SELECT launched_by, launched_at FROM runs WHERE run_id = 'run-a'")
    assert tuple(row) == ("chair-x", TS)
    assert counts(report, "runs") == (2, 2, 0, 0)


def test_an_epic_run_with_only_phase_manifests_gets_a_run_row_from_its_earliest_phase(store, tree):
    runs = tree / "runs"
    (runs / "run-e.launched.json").write_text(json.dumps({"launched_by": "chair-e", "at": TS}))
    later = {**RECORD, "run_id": "run-e:p2", "ts": "2026-09-24T15:00:00+00:00", "cartridge_sha": "late"}
    earlier = {**RECORD, "run_id": "run-e:p1", "ts": "2026-09-24T14:00:00+00:00", "cartridge_sha": "early"}
    (runs / "run-e:p2.json").write_text(json.dumps(later))
    (runs / "run-e:p1.json").write_text(json.dumps(earlier))
    report = run(store, tree)
    row = store.conn.query_one(
        "SELECT launched_by, launched_at, cartridge_sha, cartridge_team, provider_profile, principal"
        " FROM runs WHERE run_id = 'run-e'"
    )
    assert tuple(row) == ("chair-e", TS, "early", "pat", "claude-code@f9", "epic-swarm")
    assert store.conn.query_one("SELECT COUNT(*) FROM phases WHERE run_id = 'run-e'")[0] == 2
    assert counts(report, "runs") == (3, 3, 0, 0)
    assert balanced(report)


def test_usage_calls_without_ids_import_as_legacy_ids_and_a_rerun_inserts_none(store, tree):
    legacy = {"role": "scope_epic", "tier": "cheap", "model": "haiku", "cost_usd": 0.052752, "turns": 2}
    (tree / "runs" / "run-u.usage.json").write_text(json.dumps({"calls": [legacy] * 3}))
    first = run(store, tree)
    ids = store.conn.query_all("SELECT call_id, seq FROM node_calls WHERE run_id = 'run-u' ORDER BY seq")
    assert [tuple(r) for r in ids] == [("legacy:run-u:0", 0), ("legacy:run-u:1", 1), ("legacy:run-u:2", 2)]
    assert counts(first, "node_calls")[3] == 0
    second = run(store, tree)
    assert counts(second, "node_calls")[1] == 0
    assert balanced(second)


def test_archive_refuses_a_balanced_report_that_counts_malformed_records(tree):
    files = run_files(tree / "runs")
    empty = {"seen": 0, "inserted": 0, "already_present": 0, "malformed": 0}
    report = {"runs": dict(empty), "attempts": {**empty, "seen": 2, "inserted": 1, "malformed": 1}}
    assert balanced(report)
    with pytest.raises(ArchiveRefused, match="attempts=1"):
        archive_imported(files, tree / "archive", report)
    assert all(f.exists() for f in files)
    assert not (tree / "archive").exists()


def test_main_prints_the_report_and_exits_zero_when_it_balances(tree, capsys):
    argv = [str(tree / "runs"), str(tree / "work"), str(tree / "ledger.jsonl"), default_url(tree)]
    assert main(argv) == 0
    assert json.loads(capsys.readouterr().out)["node_calls"] == {
        "seen": 5,
        "inserted": 5,
        "already_present": 0,
        "malformed": 0,
    }
    assert main(argv) == 0
    assert json.loads(capsys.readouterr().out)["node_calls"]["inserted"] == 0


def test_main_exits_non_zero_when_a_row_is_neither_inserted_nor_present(tree, monkeypatch, capsys):
    """A write that is silently dropped is the case the count exists to catch."""
    monkeypatch.setattr(Store, "_insert", lambda self, table, row: 0)
    argv = [str(tree / "runs"), str(tree / "work"), str(tree / "ledger.jsonl"), default_url(tree)]
    assert main(argv) == 1
    assert json.loads(capsys.readouterr().out)["runs"]["inserted"] == 0


# ── stamping ended runs ──────────────────────────────────────────────────────


def ended(store, run_id):
    return store.conn.query_one("SELECT ended_at, status FROM runs WHERE run_id = ?", [run_id])


def test_a_run_with_a_usage_file_and_calls_is_stamped_at_its_latest_call_ts(store, tree):
    late = "2026-09-25T01:00:00+00:00"
    calls = [call("a1"), call("a2", ts=late)]
    (tree / "runs" / "run-a.calls.jsonl").write_text("\n".join(json.dumps(c) for c in calls) + "\n")
    report = run(store, tree)
    assert ended(store, "run-a") == (late, "backfilled")
    assert report["runs_stamped_ended"] == 2
    assert balanced(report)


def test_a_run_with_a_usage_file_and_no_calls_is_stamped_at_the_file_mtime(store, tree):
    usage = tree / "runs" / "run-c.usage.json"
    (tree / "runs" / "run-c.json").write_text(json.dumps({**RECORD, "run_id": "run-c"}))
    usage.write_text(json.dumps({"run_id": "run-c", "calls": []}))
    os.utime(usage, (86400.0, 86400.0))
    run(store, tree)
    assert ended(store, "run-c") == ("1970-01-02T00:00:00+00:00", "backfilled")


def test_a_run_without_a_usage_file_stays_null(store, tree):
    (tree / "runs" / "run-b.usage.json").unlink()
    report = run(store, tree)
    assert ended(store, "run-b") == (None, None)
    assert ended(store, "run-a")[1] == "backfilled"
    assert report["runs_stamped_ended"] == 1


def test_a_second_backfill_stamps_nothing_new(store, tree):
    run(store, tree)
    before = store.conn.query_all("SELECT run_id, ended_at, status FROM runs ORDER BY run_id")
    again = run(store, tree)
    assert again["runs_stamped_ended"] == 0
    assert store.conn.query_all("SELECT run_id, ended_at, status FROM runs ORDER BY run_id") == before
    assert balanced(again)


def test_a_run_already_ended_keeps_its_own_ended_at_and_status(store, tree):
    run(store, tree)
    store.finish_run("run-a", "2020-01-01T00:00:00+00:00", "completed")
    store.conn.execute("UPDATE runs SET ended_at = NULL, status = NULL WHERE run_id = 'run-b'")
    report = run(store, tree)
    assert ended(store, "run-a") == ("2020-01-01T00:00:00+00:00", "completed")
    assert ended(store, "run-b")[1] == "backfilled"
    assert report["runs_stamped_ended"] == 1


# ── archive ──────────────────────────────────────────────────────────────────


def test_an_unbalanced_report_makes_archive_refuse_and_move_nothing(tree):
    files = run_files(tree / "runs")
    archive = tree / "archive"
    unbalanced = {"runs": {"seen": 2, "inserted": 1, "already_present": 0, "malformed": 0}}
    with pytest.raises(ArchiveRefused):
        archive_imported(files, archive, unbalanced)
    assert all(f.exists() for f in files)
    assert not archive.exists()


def test_a_balanced_report_moves_the_files_and_the_originals_are_gone(store, tree):
    report = run(store, tree)
    files = run_files(tree / "runs")
    contents = {f.name: f.read_text() for f in files}
    archive = tree / "archive"
    moved = archive_imported(files, archive, report)
    assert len(moved) == len(files) == 8
    assert list((tree / "runs").iterdir()) == []
    assert {p.name: p.read_text() for p in archive.iterdir()} == contents


def test_archive_refuses_a_missing_source_or_an_existing_target_and_moves_nothing(tree):
    files = run_files(tree / "runs")
    archive = tree / "archive"
    archive.mkdir()
    (archive / files[-1].name).write_text("already here")
    balanced_report = {"runs": {"seen": 1, "inserted": 1, "already_present": 0, "malformed": 0}}
    with pytest.raises(ArchiveRefused):
        archive_imported(files, archive, balanced_report)
    with pytest.raises(ArchiveRefused):
        archive_imported([tree / "runs" / "absent.json"], archive, balanced_report)
    assert all(f.exists() for f in files)
    assert [p.name for p in archive.iterdir()] == [files[-1].name]
