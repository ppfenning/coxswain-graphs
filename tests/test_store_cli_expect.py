import json

from test_store_cli_set_state import NOW, rows, run, seed, url  # noqa: F401


def test_a_matching_expect_applies_and_exits_zero(url, run):  # noqa: F811
    seed(url, state="ready")
    code, out, err = run(url, "set-state", "i1", "t1", "done", "--by", "bob", "--expect", "ready")
    assert (code, err) == (0, "")
    assert json.loads(out)["state"] == "done"
    assert [(r["state"], r["updated_by"], r["updated_at"]) for r in rows(url, "i1")] == [("done", "bob", NOW)]


def test_a_mismatched_expect_exits_three_names_both_states_and_leaves_the_row(url, run):  # noqa: F811
    seed(url, state="ready")
    before = rows(url, "i1")
    code, out, err = run(url, "set-state", "i1", "t1", "done", "--by", "bob", "--expect", "approved")
    assert code == 3
    assert json.loads(out) == {"actual": "ready", "expected": "approved"}
    assert err == "error: work item t1 is in state ready, expected approved\n"
    assert rows(url, "i1") == before


def test_expect_on_a_missing_row_exits_three_and_inserts_nothing(url, run):  # noqa: F811
    code, out, err = run(url, "set-state", "i1", "t1", "done", "--by", "bob", "--phase", "p1", "--expect", "ready")
    assert code == 3
    assert json.loads(out) == {"actual": None, "expected": "ready"}
    assert "no row" in err
    assert rows(url, "i1") == []


def test_without_expect_the_write_stays_unconditional(url, run):  # noqa: F811
    seed(url, state="approved")
    code, _, err = run(url, "set-state", "i1", "t1", "done", "--by", "bob")
    assert (code, err) == (0, "")
    assert [r["state"] for r in rows(url, "i1")] == ["done"]


def test_an_expect_outside_the_work_states_exits_two(url, run):  # noqa: F811
    seed(url)
    code, out, err = run(url, "set-state", "i1", "t1", "done", "--by", "bob", "--expect", "planned")
    assert (code, out) == (2, "")
    assert err
    assert [r["state"] for r in rows(url, "i1")] == ["ready"]
