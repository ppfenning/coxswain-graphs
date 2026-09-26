import pytest
from conftest import T0

from harness.store_cli import Change, format_change, main, plan_regenerate, regenerate_states
from harness.store_migrate import open_store
from harness.store_read import work_items
from harness.store_write import upsert_work_item

TICKET = "---\nid: t1\nphase: p1\nstate: {state}\nneeds: []\n---\n\nBody stays.\n"


@pytest.fixture
def work(tmp_path):
    """A work root with initiative i1 holding t1 (file state ready) and t2 (no row); returns the root."""
    phase = tmp_path / "i1" / "p1"
    phase.mkdir(parents=True)
    (phase / "t1.md").write_bytes(TICKET.format(state="ready").encode())
    (phase / "t2.md").write_bytes(TICKET.replace("id: t1", "id: t2").format(state="ready").encode())
    (tmp_path / "i1" / "initiative.md").write_bytes(b"---\nstate: ready\n---\n")
    return tmp_path


def seed(conn, task="t1", state="done"):
    upsert_work_item(conn, "i1", task, "p1", state, [], T0, "alice")


def test_plan_reports_a_differing_state_and_a_row_less_ticket_and_not_agreement():
    texts = {
        "a/t1.md": TICKET.format(state="ready"),
        "a/t2.md": TICKET.replace("t1", "t2").format(state="ready"),
        "a/t3.md": TICKET.replace("t1", "t3").format(state="done"),
    }
    rows = {"t1": {"state": "done"}, "t3": {"state": "done"}}
    assert plan_regenerate(texts, rows) == [
        Change("a/t1.md", "t1", "ready", "done"),
        Change("a/t2.md", "t2", "ready", None),
    ]


def test_the_id_falls_back_to_the_file_stem_and_a_file_without_state_is_ignored():
    texts = {"a/t9.md": "---\nstate: ready\n---\n", "a/t8.md": "no frontmatter\n"}
    assert plan_regenerate(texts, {}) == [Change("a/t9.md", "t9", "ready", None)]


def test_format_names_task_and_both_states_or_the_skip():
    assert format_change(Change("n", "t1", "ready", "done")) == "t1: file state ready, store state done"
    assert format_change(Change("n", "t2", "ready", None)) == "t2: no row, skipped"


def test_a_dry_run_reports_and_leaves_every_byte_alone(store_conn, work):
    seed(store_conn)
    before = {p: p.read_bytes() for p in work.rglob("*.md")}
    assert regenerate_states(store_conn, work, "i1", False) == [
        "t1: file state ready, store state done",
        "t2: no row, skipped",
    ]
    assert {p: p.read_bytes() for p in work.rglob("*.md")} == before


def test_apply_rewrites_only_the_state_line_and_never_creates_a_row(store_conn, work):
    seed(store_conn)
    regenerate_states(store_conn, work, "i1", True)
    assert (work / "i1" / "p1" / "t1.md").read_bytes() == TICKET.format(state="done").encode()
    assert (work / "i1" / "p1" / "t2.md").read_bytes() == TICKET.replace("id: t1", "id: t2").format(
        state="ready"
    ).encode()
    assert [row["task_id"] for row in work_items(store_conn, "i1")] == ["t1"]


def test_apply_keeps_crlf_and_quotes(store_conn, work):
    seed(store_conn)
    path = work / "i1" / "p1" / "t1.md"
    path.write_bytes(b'---\r\nid: t1\r\nstate: "ready"  # note\r\n---\r\nbody\r\n')
    regenerate_states(store_conn, work, "i1", True)
    assert path.read_bytes() == b'---\r\nid: t1\r\nstate: "done"  # note\r\n---\r\nbody\r\n'


def test_agreement_prints_nothing(store_conn, work):
    seed(store_conn, "t1", "ready")
    seed(store_conn, "t2", "ready")
    assert regenerate_states(store_conn, work, "i1", True) == []


def test_a_second_apply_changes_nothing(store_conn, work):
    seed(store_conn)
    regenerate_states(store_conn, work, "i1", True)
    after_first = {p: p.read_bytes() for p in work.rglob("*.md")}
    assert regenerate_states(store_conn, work, "i1", True) == ["t2: no row, skipped"]
    assert {p: p.read_bytes() for p in work.rglob("*.md")} == after_first


def run_main(capsys, tmp_path, url, *argv):
    code = main(
        ["--store-url", url, "--runs-dir", str(tmp_path), "--provider-profile", str(tmp_path / "none.yaml"), *argv]
    )
    out, err = capsys.readouterr()
    return code, out, err


def test_main_prints_plain_lines_with_exit_zero_and_applies_only_with_the_flag(capsys, tmp_path, work):
    url = f"sqlite:///{tmp_path / 'cox.db'}"
    conn = open_store(url, T0)
    try:
        seed(conn)
    finally:
        conn.close()
    path = work / "i1" / "p1" / "t1.md"
    code, out, _ = run_main(capsys, tmp_path, url, "regenerate-states", str(work), "--initiative", "i1")
    assert (code, out) == (0, "t1: file state ready, store state done\nt2: no row, skipped\n")
    assert b"state: ready" in path.read_bytes()
    code, out, _ = run_main(capsys, tmp_path, url, "regenerate-states", str(work), "--initiative", "i1", "--apply")
    assert (code, out) == (0, "t1: file state ready, store state done\nt2: no row, skipped\n")
    assert b"state: done" in path.read_bytes()


def test_main_exits_two_with_nothing_on_stdout_for_a_missing_initiative_directory(capsys, tmp_path, work):
    code, out, err = run_main(
        capsys, tmp_path, f"sqlite:///{tmp_path / 'cox.db'}", "regenerate-states", str(work), "--initiative", "nope"
    )
    assert (code, out) == (2, "")
    assert "no directory" in err
