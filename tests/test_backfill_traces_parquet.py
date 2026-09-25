import json
from pathlib import Path

import pytest

pytest.importorskip("pyarrow")
pytest.importorskip("zstandard")

from harness import store_backfill_traces as bf
from harness import store_traces as st
from harness.traces_url import resolve_traces_root


def _events(n: int) -> list[dict]:
    return [{"type": "system", "subtype": "init"}] + [{"type": "assistant", "n": i} for i in range(1, n)]


def _never(_path: Path) -> str:
    raise AssertionError("mtime_day is only for unmatched traces")


def _loose(root: Path, run_id: str, name: str, events: list[dict]) -> Path:
    path = root / f"{run_id}-trace" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    return path


def _calls(root: Path, run_id: str, *rows: tuple[str, str, str]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"id": i, "role": "build", "ts": ts, "trace": trace}) + "\n" for i, ts, trace in rows]
    (root / f"{run_id}.calls.jsonl").write_text("".join(lines), encoding="utf-8")


def _files(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.fixture
def dirs(tmp_path):
    return tmp_path / "traces", tmp_path / "loose", tmp_path / "calls"


def _two_loose_calls(loose: Path, calls: Path) -> None:
    _loose(loose, "r1", "build-1.jsonl", _events(3))
    _loose(loose, "r1", "review-1.jsonl", _events(2))
    _calls(
        calls,
        "r1",
        ("c1", "2026-09-23T23:59:00Z", "r1-trace/build-1.jsonl"),
        ("c2", "2026-09-24T00:01:00Z", "r1-trace/review-1.jsonl"),
    )


def test_a_loose_run_converts_to_one_parquet_file_and_its_sources_are_archived(dirs):
    traces, loose, calls = dirs
    _two_loose_calls(loose, calls)
    report = bf.backfill(str(traces), loose, calls, _never)
    assert (report.runs, report.sources, report.rows, report.archived, report.mismatches) == (1, 2, 5, 2, ())
    assert sorted(_files(traces)) == [
        "2026/09/23/r1.parquet",
        "archive/2026/09/23/r1-trace/build-1.jsonl",
        "archive/2026/09/24/r1-trace/review-1.jsonl",
    ]
    assert st.read_call(str(traces), "r1", "c1") == _events(3)
    assert st.read_call(str(traces), "r1", "c2") == _events(2)
    assert list(loose.rglob("*.jsonl")) == []


def test_an_unmatched_loose_trace_takes_the_synthesised_call_id_and_mtime_day(dirs):
    traces, loose, _ = dirs
    _loose(loose, "r9", "build-2.jsonl", _events(2))
    report = bf.backfill(str(traces), loose, None, lambda p: "2026-09-25")
    assert report.archived == 1
    assert st.read_call(str(traces), "r9", "r9-build-2") == _events(2)
    assert (traces / "2026/09/25/r9.parquet").is_file()


def test_a_legacy_day_file_converts_and_is_archived_under_its_date(dirs):
    traces, _, _ = dirs
    st.append_call(traces, "2026-09-24", "r5", "c7", _events(3))
    st.append_call(traces, "2026-09-24", "r5", "c8", _events(2))
    report = bf.backfill(str(traces), None, None, _never)
    assert (report.runs, report.sources, report.rows, report.archived) == (1, 1, 5, 1)
    assert sorted(_files(traces)) == ["2026/09/24/r5.parquet", "archive/2026/09/24/r5.jsonl.zst"]
    assert st.read_call(str(traces), "r5", "c7") == _events(3)
    assert len(list(st.read_legacy_file(traces / "archive/2026/09/24/r5.jsonl.zst"))) == 5


def test_a_run_with_both_sources_merges_into_one_file_on_the_earliest_day(dirs):
    traces, loose, calls = dirs
    st.append_call(traces, "2026-09-23", "r1", "c0", _events(2))
    _loose(loose, "r1", "build-1.jsonl", _events(3))
    _calls(calls, "r1", ("c1", "2026-09-24T10:00:00Z", "r1-trace/build-1.jsonl"))
    report = bf.backfill(str(traces), loose, calls, _never)
    assert (report.runs, report.sources, report.rows, report.archived) == (1, 2, 5, 2)
    assert [p for p in _files(traces) if p.endswith(".parquet")] == ["2026/09/23/r1.parquet"]
    assert st.read_call(str(traces), "r1", "c0") == _events(2)
    assert st.read_call(str(traces), "r1", "c1") == _events(3)
    assert len(list(st.iter_run(str(traces), "r1"))) == 5


def test_a_call_held_by_both_sources_with_equal_events_counts_once(dirs):
    traces, loose, calls = dirs
    st.append_call(traces, "2026-09-24", "r1", "c1", _events(3))
    _loose(loose, "r1", "build-1.jsonl", _events(3))
    _calls(calls, "r1", ("c1", "2026-09-24T10:00:00Z", "r1-trace/build-1.jsonl"))
    report = bf.backfill(str(traces), loose, calls, _never)
    assert (report.rows, report.archived, report.mismatches) == (3, 2, ())


def test_a_call_whose_events_differ_between_sources_is_a_conflict_and_nothing_is_written(dirs, capsys):
    traces, loose, calls = dirs
    st.append_call(traces, "2026-09-24", "r1", "c1", _events(2))
    _loose(loose, "r1", "build-1.jsonl", _events(3))
    _calls(calls, "r1", ("c1", "2026-09-24T10:00:00Z", "r1-trace/build-1.jsonl"))
    before = _files(traces.parent)
    report = bf.backfill(str(traces), loose, calls, _never)
    assert (report.conflicts, report.mismatches, report.archived) == ((bf.Conflict("r1", ("c1",)),), (), 0)
    assert _files(traces.parent) == before
    assert bf.main([str(traces), "--loose-root", str(loose), "--calls-dir", str(calls)]) == 1
    assert "conflict: run r1 calls c1 differ between sources; nothing written" in capsys.readouterr().out


def test_a_stale_parquet_on_another_day_fails_the_real_read_back(dirs):
    traces, loose, calls = dirs
    root = resolve_traces_root(str(traces), Path("."), {})
    st.write_run(root, "2026-09-22", "r1", {"c1": _events(1)})
    st.write_run(root, "2026-09-30", "r1", {"c1": _events(1)})
    _loose(loose, "r1", "build-1.jsonl", _events(3))
    _calls(calls, "r1", ("c1", "2026-09-23T10:00:00Z", "r1-trace/build-1.jsonl"))
    report = bf.backfill(str(traces), loose, calls, _never)
    assert (report.mismatches, report.archived) == ((bf.Mismatch("r1", 3, 4),), 0)
    assert (loose / "r1-trace/build-1.jsonl").exists()
    assert [p for p in _files(traces) if p.endswith(".parquet")] == ["2026/09/22/r1.parquet", "2026/09/30/r1.parquet"]


def test_a_rerun_after_a_partial_archive_keeps_one_parquet_file(dirs, monkeypatch):
    traces, loose, calls = dirs
    st.append_call(traces, "2026-09-23", "r1", "c0", _events(2))
    _loose(loose, "r1", "build-1.jsonl", _events(3))
    _calls(calls, "r1", ("c1", "2026-09-24T10:00:00Z", "r1-trace/build-1.jsonl"))
    real, moved = bf.shutil.move, []

    def move_once(src, dst):
        if moved:
            raise OSError("interrupted")
        moved.append(src)
        return real(src, dst)

    with monkeypatch.context() as m:
        m.setattr(bf.shutil, "move", move_once)
        with pytest.raises(OSError):
            bf.backfill(str(traces), loose, calls, _never)
    assert (traces / "archive/2026/09/23/r1.jsonl.zst").is_file()
    assert (loose / "r1-trace/build-1.jsonl").is_file()
    report = bf.backfill(str(traces), loose, calls, _never)
    assert (report.archived, report.mismatches) == (1, ())
    assert [p for p in _files(traces) if p.endswith(".parquet")] == ["2026/09/23/r1.parquet"]
    assert len(list(st.iter_run(str(traces), "r1"))) == 5
    assert st.read_call(str(traces), "r1", "c1") == _events(3)


def test_a_count_mismatch_archives_nothing_keeps_the_parquet_and_reports_both_counts(dirs, monkeypatch, capsys):
    traces, loose, calls = dirs
    _two_loose_calls(loose, calls)
    sources = _files(loose)
    real = st.iter_run
    monkeypatch.setattr(st, "iter_run", lambda root, run_id: list(real(root, run_id))[:-1])
    report = bf.backfill(str(traces), loose, calls, _never)
    assert report.mismatches == (bf.Mismatch("r1", 5, 4),)
    assert report.archived == 0
    assert _files(loose) == sources
    assert not (traces / "archive").exists()
    assert (traces / "2026/09/23/r1.parquet").is_file()
    args = [str(traces), "--loose-root", str(loose), "--calls-dir", str(calls)]
    assert bf.main(args) == 1
    assert "mismatch: run r1 expected 5 events, read back 4" in capsys.readouterr().out


def test_a_rerun_after_a_mismatch_finishes_the_run_from_the_kept_parquet(dirs, monkeypatch):
    traces, loose, calls = dirs
    _two_loose_calls(loose, calls)
    real = st.iter_run
    with monkeypatch.context() as m:
        m.setattr(st, "iter_run", lambda root, run_id: list(real(root, run_id))[:-1])
        assert bf.backfill(str(traces), loose, calls, _never).archived == 0
    report = bf.backfill(str(traces), loose, calls, _never)
    assert (report.archived, report.mismatches) == (2, ())
    assert len(list(st.iter_run(str(traces), "r1"))) == 5


def test_a_rerun_is_a_no_op(dirs):
    traces, loose, calls = dirs
    _two_loose_calls(loose, calls)
    st.append_call(traces, "2026-09-24", "r5", "c7", _events(3))
    assert bf.backfill(str(traces), loose, calls, _never).archived == 3
    before = _files(traces.parent)
    assert bf.backfill(str(traces), loose, calls, _never) == bf.Report()
    assert _files(traces.parent) == before


def test_an_archive_destination_already_taken_blocks_the_move_and_fails_the_run(dirs, capsys):
    traces, loose, calls = dirs
    _two_loose_calls(loose, calls)
    taken = traces / "archive/2026/09/23/r1-trace/build-1.jsonl"
    taken.parent.mkdir(parents=True)
    taken.write_text("keep me")
    report = bf.backfill(str(traces), loose, calls, _never)
    assert (report.archived, report.blocked) == (0, 2)
    assert taken.read_text() == "keep me" and len(list(loose.rglob("*.jsonl"))) == 2
    args = [str(traces), "--loose-root", str(loose), "--calls-dir", str(calls)]
    assert bf.main(args) == 1


def test_an_empty_loose_file_is_counted_archived_with_its_run_and_a_rerun_is_clean(dirs):
    traces, loose, calls = dirs
    _two_loose_calls(loose, calls)
    _loose(loose, "r1", "build-2.jsonl", [])
    report = bf.backfill(str(traces), loose, calls, lambda p: "2026-09-25")
    assert (report.empty, report.archived, report.mismatches) == (1, 3, ())
    assert (traces / "archive/2026/09/25/r1-trace/build-2.jsonl").read_text() == ""
    assert bf.main([str(traces), "--loose-root", str(loose), "--calls-dir", str(calls)]) == 0


def test_without_zstandard_legacy_files_are_left_and_a_warning_is_printed(dirs, monkeypatch, capsys):
    traces, loose, calls = dirs
    _two_loose_calls(loose, calls)
    st.append_call(traces, "2026-09-24", "r5", "c7", _events(3))
    before = _files(traces.parent)

    def missing():
        raise st.TracesUnavailable("no zstandard")

    monkeypatch.setattr(st, "_zstd", missing)
    assert bf.backfill(str(traces), loose, calls, _never) == bf.Report(unavailable=True)
    assert "zstandard" in capsys.readouterr().err
    assert _files(traces.parent) == before
    assert bf.main([str(traces), "--loose-root", str(loose)]) == 1


def test_without_pyarrow_nothing_is_touched_and_a_warning_is_printed(dirs, monkeypatch, capsys):
    traces, loose, calls = dirs
    _two_loose_calls(loose, calls)
    st.append_call(traces, "2026-09-24", "r5", "c7", _events(3))
    before = _files(traces.parent)
    monkeypatch.setattr(bf, "have_pyarrow", lambda: False)
    assert bf.backfill(str(traces), loose, calls, _never) == bf.Report(unavailable=True)
    assert "pyarrow" in capsys.readouterr().err
    assert _files(traces.parent) == before
    assert bf.main([str(traces), "--loose-root", str(loose)]) == 1


def test_legacy_discovery_lists_year_month_day_only(tmp_path):
    for rel in (
        "2026/09/25/a.jsonl.zst",
        "archive/2026/09/25/b.jsonl.zst",
        "foo/bar/baz/c.jsonl.zst",
        "2026/09/d.jsonl.zst",
    ):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_bytes(b"")
    assert [p.relative_to(tmp_path).as_posix() for p in bf.find_legacy_files(tmp_path)] == ["2026/09/25/a.jsonl.zst"]


def test_a_url_with_credentials_is_refused_and_never_echoed(capsys):
    assert bf.main(["s3://key:sekret@bucket/prefix"]) == 2
    captured = capsys.readouterr()
    assert "sekret" not in captured.out + captured.err
    assert "s3://bucket/prefix" in captured.err
