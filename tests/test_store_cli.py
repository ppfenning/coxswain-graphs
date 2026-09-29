import json
import os
import uuid

import pytest
from conftest import T0, with_search_path

import harness.store_cli as store_cli
from harness.cli import REPO_ROOT
from harness.store_cli import main, resolve_store_url
from harness.store_migrate import open_store
from harness.store_pause import is_paused
from harness.store_read import task_record
from harness.store_write import Store

PR = "https://example.test/pr/1"
AT = "2026-09-25T00:00:00Z"
LONG = "3600"
T20 = "2026-09-24T00:00:20Z"
T40 = "2026-09-24T00:00:40Z"


@pytest.fixture(params=["sqlite", "postgres"])
def url(request, tmp_path):
    """A store URL a second connection can reach: a sqlite file, or a Postgres schema of its own."""
    if request.param == "sqlite":
        yield f"sqlite:///{tmp_path / 'cox.db'}"
        return
    base = os.environ.get("COXSWAIN_TEST_PG_URL")
    if not base:
        pytest.skip("COXSWAIN_TEST_PG_URL is not set")
    import psycopg

    schema = f"t_{uuid.uuid4().hex}"
    admin = psycopg.connect(base, autocommit=True)
    try:
        admin.execute(f"CREATE SCHEMA {schema}")
        yield with_search_path(base, schema)
    finally:
        try:
            admin.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        finally:
            admin.close()


@pytest.fixture
def run(capsys, tmp_path):
    """Call main against a store URL; the code, the stdout and the stderr."""

    def call(store_url, *argv):
        code = main(["--store-url", store_url, "--runs-dir", str(tmp_path), "--provider-profile", str(tmp_path / "none.yaml"), *argv])
        out, err = capsys.readouterr()
        return code, out, err

    return call


@pytest.fixture
def at(monkeypatch):
    """Pin the clock main reads."""
    return lambda now: monkeypatch.setattr(store_cli, "_now", lambda: now)


def with_conn(url, action):
    conn = open_store(url, T0)
    try:
        return action(conn)
    finally:
        conn.close()


def seed(url, record=None):
    with_conn(url, lambda c: Store(c).record_task_record("r1", "p1", "t1", {"n": "x"} if record is None else record, T0))


def _insert_run(conn, run_id):
    p = conn.dialect.placeholder
    conn.execute(f"INSERT INTO runs (run_id, status) VALUES ({p}, {p})", (run_id, "queued"))


def seed_run(url, run_id="r1"):
    with_conn(url, lambda c: _insert_run(c, run_id))


def one_object(out):
    assert out.endswith("\n") and out.count("\n") == 1
    return json.loads(out)


def test_mark_landed_prints_and_stores_the_record_with_the_landed_object(url, run):
    seed(url, record={"a": 1, "b": 2})
    code, out, err = run(url, "mark-landed", "r1", "p1", "t1", "--pr", PR, "--at", AT)
    expected = {"a": 1, "b": 2, "landed": {"pr": PR, "at": AT}}
    assert (code, one_object(out), err) == (0, expected, "")
    assert with_conn(url, lambda c: task_record(c, "r1", "p1", "t1")) == expected


def test_lease_acquire_prints_exactly_ok_epoch_holder(url, run):
    code, out, err = run(url, "lease", "acquire", "chair", "a", "--ttl", LONG)
    assert (code, one_object(out), err) == (0, {"ok": True, "epoch": 1, "holder": "a"}, "")


def test_lease_renew_prints_exactly_ok_epoch_holder_and_extends_the_lease(url, run, at):
    at(T0)
    run(url, "lease", "acquire", "chair", "a", "--ttl", "30")
    at(T20)
    code, out, _ = run(url, "lease", "renew", "chair", "a", "1", "--ttl", "30")
    assert (code, one_object(out)) == (0, {"ok": True, "epoch": None, "holder": None})
    at(T40)
    code, out, _ = run(url, "lease", "acquire", "chair", "b", "--ttl", "30")
    assert (code, one_object(out)) == (3, {"ok": False, "epoch": 1, "holder": "a"})


def test_lease_release_prints_exactly_ok_epoch_holder_and_frees_the_name(url, run):
    run(url, "lease", "acquire", "chair", "a", "--ttl", LONG)
    code, out, _ = run(url, "lease", "release", "chair", "a", "1")
    assert (code, one_object(out)) == (0, {"ok": True, "epoch": None, "holder": None})
    code, out, _ = run(url, "lease", "acquire", "chair", "b", "--ttl", LONG)
    assert (code, one_object(out)) == (0, {"ok": True, "epoch": 2, "holder": "b"})


ACTION = json.dumps(
    {"ts": "2026-09-26T00:00:00Z", "epoch": 4, "kind": "land", "status": "done", "reason": "merged", "task_id": "t1"}
)


def test_record_action_writes_one_row_with_the_target_and_a_repeat_writes_nothing_more(url, run):
    code, out, err = run(url, "record-action", "--holder", "chair-a", ACTION)
    assert (code, out, err) == (0, "recorded chair action land t1\n", "")
    rows = "SELECT ts, epoch, holder, kind, target, status, reason FROM chair_actions"
    expected = [("2026-09-26T00:00:00Z", 4, "chair-a", "land", "t1", "done", "merged")]
    assert with_conn(url, lambda c: c.query_all(rows)) == expected
    assert run(url, "record-action", "--holder", "chair-a", ACTION)[0] == 0
    assert with_conn(url, lambda c: c.query_all(rows)) == expected


def test_record_action_target_falls_back_to_initiative_then_the_first_intake_id_then_empty(url, run):
    base = {"ts": "2026-09-26T00:00:00Z", "epoch": 4, "status": "recorded"}
    lines = [
        {**base, "kind": "launch_epic", "initiative": "i1", "intake_ids": ["a"]},
        {**base, "kind": "launch_decompose", "intake_ids": ["a", "b"]},
        {**base, "kind": "standby"},
    ]
    outs = [run(url, "record-action", "--holder", "h", json.dumps(line))[1] for line in lines]
    assert outs == [
        "recorded chair action launch_epic i1\n",
        "recorded chair action launch_decompose a\n",
        "recorded chair action standby \n",
    ]


def test_pause_prints_ok_and_sets_paused_at(url, run, at):
    seed_run(url)
    at(T0)
    code, out, err = run(url, "pause", "r1", "--reason", "manual")
    expected = {"ok": True, "run": "r1", "paused": T0, "reason": "manual"}
    assert (code, one_object(out), err) == (0, expected, "")
    assert with_conn(url, lambda c: is_paused(c, "r1")) is True


def test_resume_clears_the_paused_flag(url, run, at):
    seed_run(url)
    at(T0)
    run(url, "pause", "r1")
    code, out, err = run(url, "resume", "r1")
    assert (code, one_object(out), err) == (0, {"ok": True, "run": "r1", "paused": None}, "")
    assert with_conn(url, lambda c: is_paused(c, "r1")) is False


def test_pause_and_resume_on_an_unknown_run_exit_3_and_leave_other_runs_alone(url, run, at):
    seed_run(url)
    at(T0)
    run(url, "pause", "r1")
    rows = "SELECT run_id, paused_at FROM runs ORDER BY run_id"
    expected = {"ok": False, "run": "no-such-run", "paused": None, "reason": None}
    for argv in (("pause", "no-such-run"), ("resume", "no-such-run")):
        at(T40)
        code, out, err = run(url, *argv)
        assert (code, one_object(out)) == (3, expected)
        assert err == "error: no run no-such-run\n"
        assert with_conn(url, lambda c: c.query_all(rows)) == [("r1", T0)]


def test_exit_two_on_an_action_line_without_kind_and_nothing_is_written(url, run):
    line = json.dumps({"ts": "2026-09-26T00:00:00Z", "epoch": 4, "status": "done"})
    code, out, err = run(url, "record-action", "--holder", "chair-a", line)
    assert (code, out) == (2, "")
    assert err == "error: the action lacks kind\n"
    assert with_conn(url, lambda c: c.query_all("SELECT COUNT(*) FROM chair_actions")) == [(0,)]


def test_exit_two_on_an_action_line_that_is_not_json(run, tmp_path):
    code, out, err = run(f"sqlite:///{tmp_path / 'cox.db'}", "record-action", "--holder", "chair-a", "{nope")
    assert (code, out) == (2, "")
    assert err.startswith("error: the action is not JSON")
    assert not (tmp_path / "cox.db").exists()


def test_exit_three_when_mark_landed_finds_no_record_prints_the_empty_object(url, run):
    code, out, err = run(url, "mark-landed", "r1", "p1", "missing", "--pr", PR, "--at", AT)
    assert (code, one_object(out)) == (3, {})
    assert err.startswith("error: no task record")


def test_exit_three_when_a_lease_is_held_by_another(url, run):
    run(url, "lease", "acquire", "chair", "a", "--ttl", LONG)
    code, out, err = run(url, "lease", "acquire", "chair", "b", "--ttl", LONG)
    assert (code, one_object(out)) == (3, {"ok": False, "epoch": 1, "holder": "a"})
    assert err.startswith("error: lease chair refused")


def test_exit_three_on_a_renew_with_a_stale_epoch(url, run):
    run(url, "lease", "acquire", "chair", "a", "--ttl", LONG)
    code, out, _ = run(url, "lease", "renew", "chair", "a", "99", "--ttl", LONG)
    assert (code, one_object(out)) == (3, {"ok": False, "epoch": None, "holder": None})


def test_exit_three_on_a_release_by_the_wrong_holder_and_the_lease_stands(url, run):
    run(url, "lease", "acquire", "chair", "a", "--ttl", LONG)
    code, out, _ = run(url, "lease", "release", "chair", "b", "1")
    assert (code, one_object(out)) == (3, {"ok": False, "epoch": None, "holder": None})
    code, out, _ = run(url, "lease", "acquire", "chair", "b", "--ttl", LONG)
    assert (code, one_object(out)) == (3, {"ok": False, "epoch": 1, "holder": "a"})


def test_exit_two_when_the_store_cannot_be_opened(run, tmp_path):
    code, out, err = run(f"sqlite:///{tmp_path / 'no' / 'such' / 'cox.db'}", "lease", "release", "chair", "a", "1")
    assert (code, out) == (2, "")
    assert err.startswith("error: cannot open the store")


def test_exit_two_on_an_unsupported_store_url(run):
    code, out, err = run("mysql://h/db", "lease", "release", "chair", "a", "1")
    assert (code, out) == (2, "")
    assert "unsupported store URL" in err


def test_exit_two_when_the_store_opens_but_cannot_be_read(url, run):
    with_conn(url, lambda c: c.execute("DROP TABLE task_records"))
    code, out, err = run(url, "mark-landed", "r1", "p1", "t1", "--pr", PR, "--at", AT)
    assert (code, out) == (2, "")
    assert err.startswith("error: cannot read the store")


@pytest.mark.parametrize("ttl", ["0", "-5", "ten"])
def test_exit_two_on_a_ttl_that_is_not_a_positive_integer_and_the_store_is_untouched(run, tmp_path, ttl):
    code, out, err = run(f"sqlite:///{tmp_path / 'cox.db'}", "lease", "acquire", "chair", "a", "--ttl", ttl)
    assert (code, out) == (2, "")
    assert "--ttl" in err
    assert not (tmp_path / "cox.db").exists()


def test_exit_two_on_an_at_that_is_not_iso_and_the_record_is_unchanged(url, run):
    seed(url)
    code, out, err = run(url, "mark-landed", "r1", "p1", "t1", "--pr", PR, "--at", "next tuesday")
    assert (code, out) == (2, "")
    assert "not an ISO 8601 time" in err
    assert with_conn(url, lambda c: task_record(c, "r1", "p1", "t1")) == {"n": "x"}


def test_exit_two_on_a_non_integer_epoch(capsys):
    assert main(["lease", "renew", "chair", "a", "not-a-number", "--ttl", "5"]) == 2
    out, err = capsys.readouterr()
    assert out == "" and "invalid int value" in err


def test_help_goes_to_stderr_and_stdout_stays_empty(capsys, monkeypatch):
    # Python 3.14 argparse can colour help by environment, which puts escape codes before "usage:".
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.delenv("PYTHON_COLORS", raising=False)
    assert main(["--help"]) == 0
    out, err = capsys.readouterr()
    assert out == "" and err.startswith("usage:")


def test_a_handler_bug_raises_instead_of_hiding_behind_exit_two(run, tmp_path, monkeypatch):
    def broken(*_):
        raise ValueError("bug")

    monkeypatch.setattr(store_cli, "mark_landed", broken)
    with pytest.raises(ValueError, match="bug"):
        run(f"sqlite:///{tmp_path / 'cox.db'}", "mark-landed", "r1", "p1", "t1", "--pr", PR, "--at", AT)


def test_the_store_flag_is_accepted_after_the_subcommand(tmp_path, capsys):
    url = f"sqlite:///{tmp_path / 'cox.db'}"
    code = main(["lease", "acquire", "chair", "a", "--ttl", LONG, "--store-url", url, "--provider-profile", str(tmp_path / "none.yaml")])
    assert (code, one_object(capsys.readouterr().out)["ok"]) == (0, True)


def test_neither_store_url_nor_runs_dir_exits_2_and_creates_no_file(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    before = sorted(REPO_ROOT.rglob("cox.db"))
    code = main(["lease", "acquire", "chair", "a", "--ttl", LONG])
    out, err = capsys.readouterr()
    assert (code, out, err) == (2, "", "error: --store-url or --runs-dir is required\n")
    assert list(tmp_path.rglob("cox.db")) == []
    assert sorted(REPO_ROOT.rglob("cox.db")) == before


def test_runs_dir_alone_opens_cox_db_in_it(tmp_path, capsys):
    code = main(["--runs-dir", str(tmp_path), "lease", "acquire", "chair", "a", "--ttl", LONG])
    assert (code, one_object(capsys.readouterr().out)["ok"]) == (0, True)
    assert (tmp_path / "cox.db").is_file()


def test_runs_dir_is_accepted_after_the_subcommand(tmp_path, capsys):
    code = main(["lease", "acquire", "chair", "a", "--ttl", LONG, "--runs-dir", str(tmp_path)])
    assert (code, one_object(capsys.readouterr().out)["ok"]) == (0, True)
    assert (tmp_path / "cox.db").is_file()


def test_the_flag_wins_over_the_profile_and_the_runs_dir():
    assert resolve_store_url("sqlite:///a.db", {"storage_url": "postgresql://h/db"}, "/runs") == "sqlite:///a.db"


def test_the_profile_wins_over_the_runs_dir_default():
    assert resolve_store_url(None, {"storage_url": "postgresql://h/db"}, "/runs") == "postgresql://h/db"


def test_without_a_flag_or_a_profile_value_the_store_is_cox_db_in_the_runs_dir():
    assert resolve_store_url(None, {}, "/runs") == "sqlite:////runs/cox.db"


def test_an_empty_flag_and_an_empty_profile_value_fall_through():
    assert resolve_store_url("", {"storage_url": ""}, "/runs") == "sqlite:////runs/cox.db"


VERSIONS = '{"cox": "0.20.0", "login_ok": true}'


def test_host_beat_sets_beat_at_and_versions_on_an_upserted_host(url, run, at):
    at(T20)
    code, out, _ = run(url, "host", "upsert", "jarvis", "--ssh", "jarvis", "--capacity", "8", "--by", "chair")
    assert (code, one_object(out)["beat_at"]) == (0, "")
    at(T40)
    code, out, _ = run(url, "host", "beat", "jarvis", "--versions", VERSIONS)
    assert (code, one_object(out)) == (
        0,
        {
            "name": "jarvis",
            "ssh": "jarvis",
            "capacity": 8,
            "state": "active",
            "beat_at": T40,
            "versions_json": {"cox": "0.20.0", "login_ok": True},
            "updated_at": T20,
            "updated_by": "chair",
        },
    )


def test_host_set_state_changes_only_state_updated_at_and_updated_by(url, run, at):
    at(T20)
    run(url, "host", "upsert", "jarvis", "--ssh", "jarvis", "--capacity", "8", "--by", "chair")
    run(url, "host", "beat", "jarvis", "--versions", VERSIONS)
    _, before, _ = run(url, "host", "list")
    at(T40)
    code, out, _ = run(url, "host", "set-state", "jarvis", "draining", "--by", "pat")
    assert code == 0
    changed = {"state": "draining", "updated_at": T40, "updated_by": "pat"}
    assert one_object(out) == {**json.loads(before)[0], **changed}


def test_host_set_state_outside_the_three_exits_two_with_nothing_on_stdout(url, run):
    code, out, _ = run(url, "host", "set-state", "jarvis", "parked", "--by", "chair")
    assert (code, out) == (2, "")


def test_host_beat_on_an_unknown_host_exits_three_with_the_empty_object(url, run):
    code, out, err = run(url, "host", "beat", "nobody", "--versions", "{}")
    assert (code, one_object(out)) == (3, {})
    assert err.startswith("error: no host nobody")


def test_host_list_prints_every_host_by_name(url, run):
    for name in ("b", "a"):
        run(url, "host", "upsert", name, "--ssh", name, "--capacity", "1", "--by", "chair")
    code, out, _ = run(url, "host", "list")
    assert (code, [r["name"] for r in one_object(out)]) == (0, ["a", "b"])
