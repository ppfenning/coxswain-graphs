import json
import os
import uuid

import pytest
from conftest import T0, with_search_path

import harness.store_cli as store_cli
from harness.store_cli import main, resolve_store_url
from harness.store_migrate import open_store
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


def test_help_goes_to_stderr_and_stdout_stays_empty(capsys):
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


def test_the_flag_wins_over_the_profile_and_the_runs_dir():
    assert resolve_store_url("sqlite:///a.db", {"storage_url": "postgresql://h/db"}, "/runs") == "sqlite:///a.db"


def test_the_profile_wins_over_the_runs_dir_default():
    assert resolve_store_url(None, {"storage_url": "postgresql://h/db"}, "/runs") == "postgresql://h/db"


def test_without_a_flag_or_a_profile_value_the_store_is_cox_db_in_the_runs_dir():
    assert resolve_store_url(None, {}, "/runs") == "sqlite:////runs/cox.db"


def test_an_empty_flag_and_an_empty_profile_value_fall_through():
    assert resolve_store_url("", {"storage_url": ""}, "/runs") == "sqlite:////runs/cox.db"
