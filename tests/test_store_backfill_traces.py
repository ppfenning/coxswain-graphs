import json
from pathlib import Path

import pytest

pytest.importorskip("zstandard")

from harness import store_backfill_traces as bf
from harness import store_traces as st

INIT = {"type": "system", "subtype": "init", "model": "claude-haiku-4-5-20251001", "claude_code_version": "2.1.280"}


def _events(n: int) -> list[dict]:
    return [INIT] + [{"type": "assistant", "n": i} for i in range(1, n)]


def _write_trace(loose: Path, run_id: str, name: str, events: list[dict]) -> Path:
    path = loose / f"{run_id}-trace" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    return path


def _write_calls(calls: Path, run_id: str, rows: list[dict]) -> None:
    calls.mkdir(parents=True, exist_ok=True)
    (calls / f"{run_id}.calls.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


@pytest.fixture
def tree(tmp_path):
    loose, calls = tmp_path / "loose", tmp_path / "calls"
    _write_trace(loose, "r1", "build-1.jsonl", _events(3))
    _write_trace(loose, "r1", "review-1.jsonl", _events(2))
    _write_trace(loose, "r2", "build-1.jsonl", _events(4))
    _write_calls(
        calls,
        "r1",
        [
            {"id": "c1", "role": "build", "ts": "2026-09-23T23:59:00Z", "trace": "traces/r1-trace/build-1.jsonl"},
            {"id": "c2", "role": "review", "ts": "2026-09-24T00:01:00Z", "trace": "traces/r1-trace/review-1.jsonl"},
        ],
    )
    _write_calls(
        calls, "r2", [{"id": "c3", "role": "build", "ts": "2026-09-24T10:00:00Z", "trace": "r2-trace/build-1.jsonl"}]
    )
    return loose, calls, tmp_path / "new", tmp_path / "archive"


def _never(_path: Path) -> str:
    raise AssertionError("mtime_day is only for unmatched traces")


def test_parse_trace_name_reads_role_and_one_based_index():
    assert bf.parse_trace_name("build-2.jsonl") == ("build", 2)
    assert bf.parse_trace_name("review-pass-10.jsonl") == ("review-pass", 10)
    assert bf.parse_trace_name("notes.txt") is None


def test_plan_moves_matches_on_run_and_final_file_name():
    calls = [bf.Call("r1", "c1", "build", "2026-09-23T01:00:00Z", "x/y/build-1.jsonl")]
    a = bf.TraceFile("r1", "build", 1, Path("/l/r1-trace/build-1.jsonl"))
    b = bf.TraceFile("r2", "build", 1, Path("/l/r2-trace/build-1.jsonl"))
    plan = bf.plan_moves(calls, [a, b], lambda p: "2026-01-02")
    assert plan[a] == bf.Move("2026-09-23", "r1", "c1", True)
    assert plan[b] == bf.Move("2026-01-02", "r2", "r2-build-1", False)


def test_loaders_read_the_fixture_tree(tree):
    loose, calls, _, _ = tree
    assert [(t.run_id, t.role, t.index) for t in bf.find_trace_files(loose)] == [
        ("r1", "build", 1),
        ("r1", "review", 1),
        ("r2", "build", 1),
    ]
    assert [c.id for c in bf.load_calls(calls)] == ["c1", "c2", "c3"]
    assert bf.read_events(loose / "r1-trace" / "build-1.jsonl") == _events(3)


def test_three_traces_across_two_days_land_in_two_day_directories(tree):
    loose, calls, new, _ = tree
    report = bf.import_all(loose, calls, new, None, _never)
    assert (report.seen, report.appended, report.present, report.unmatched) == (3, 3, 0, 0)
    assert sorted(p.relative_to(new).as_posix() for p in new.rglob("*") if p.is_file()) == [
        "2026/09/23/r1.jsonl.zst",
        "2026/09/24/r1.jsonl.zst",
        "2026/09/24/r2.jsonl.zst",
    ]
    assert sorted(p.relative_to(new).as_posix() for p in new.glob("*/*/*") if p.is_dir()) == [
        "2026/09/23",
        "2026/09/24",
    ]
    assert st.read_call(new, "r1", "c1") == _events(3)
    assert st.read_call(new, "r2", "c3") == _events(4)
    assert report.src_bytes > 0 and report.dst_bytes > 0


def test_a_rerun_appends_nothing(tree):
    loose, calls, new, _ = tree
    bf.import_all(loose, calls, new, None, _never)
    before = {p: p.read_bytes() for p in new.rglob("*.zst")}
    report = bf.import_all(loose, calls, new, None, _never)
    assert (report.seen, report.appended, report.present) == (3, 0, 3)
    assert {p: p.read_bytes() for p in new.rglob("*.zst")} == before


def test_an_unmatched_trace_imports_under_the_synthesised_id(tree):
    loose, calls, new, _ = tree
    _write_trace(loose, "r9", "build-2.jsonl", _events(2))
    report = bf.import_all(loose, calls, new, None, lambda p: "2026-09-25")
    assert (report.seen, report.appended, report.unmatched) == (4, 4, 1)
    assert st.read_call(new, "r9", "r9-build-2") == _events(2)
    assert (new / "2026" / "09" / "25" / "r9.jsonl.zst").is_file()


def test_a_file_whose_read_back_count_differs_is_not_archived(tree):
    loose, calls, new, archive = tree
    st.append_call(new, "2026-09-23", "r1", "c1", _events(3)[:2])
    report = bf.import_all(loose, calls, new, archive, _never)
    assert (report.appended, report.present, report.archived) == (2, 1, 2)
    assert (loose / "r1-trace" / "build-1.jsonl").exists()
    assert not (archive / "r1-trace" / "build-1.jsonl").exists()


def test_archive_moves_verified_sources_and_never_deletes(tree):
    loose, calls, new, archive = tree
    report = bf.import_all(loose, calls, new, archive, _never)
    assert report.archived == 3
    assert list(loose.rglob("*.jsonl")) == []
    assert sorted(p.relative_to(archive).as_posix() for p in archive.rglob("*.jsonl")) == [
        "r1-trace/build-1.jsonl",
        "r1-trace/review-1.jsonl",
        "r2-trace/build-1.jsonl",
    ]
    assert bf.read_events(archive / "r2-trace" / "build-1.jsonl") == _events(4)


def test_main_reports_counts_and_exits_zero(tree, capsys):
    loose, calls, new, _ = tree
    assert bf.main([str(loose), str(calls), str(new)]) == 0
    out = capsys.readouterr().out
    assert "seen: 3" in out and "appended: 3" in out and "present: 0" in out


def test_main_fails_when_a_file_cannot_be_imported(tree, capsys):
    loose, calls, new, _ = tree
    _write_trace(loose, "r1", "build-2.jsonl", [])
    assert bf.main([str(loose), str(calls), str(new)]) == 1
    assert "seen: 4" in capsys.readouterr().out
