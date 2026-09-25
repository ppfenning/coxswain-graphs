"""Run-end compaction: one Parquet file per run under the traces root, loose files deleted only on a matching count."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness import cli, store_traces

RUN = "runP"
EVENTS = [{"type": "system"}, {"type": "assistant"}, {"type": "result"}]


def _stage(monkeypatch, runs_dir: Path, n: int = 2) -> list[Path]:
    """Write n loose trace files and point node_calls at them. Returns the files."""
    trace_dir = runs_dir / f"{RUN}-trace"
    trace_dir.mkdir(parents=True, exist_ok=True)
    files = [trace_dir / f"c{i}.jsonl" for i in range(1, n + 1)]
    for file in files:
        file.write_text("\n".join(json.dumps(e) for e in EVENTS) + "\n", encoding="utf-8")
    rows = [
        {"call_id": f"call-{i}", "ts": "2026-09-25T07:20:00+00:00", "detail_json": json.dumps({"trace": str(f)})}
        for i, f in enumerate(files, 1)
    ]
    monkeypatch.setattr(cli, "node_calls", lambda conn, run_id: rows)
    return files


def _compact(runs_dir: Path, url: str | None = None) -> None:
    cli._compact_traces(SimpleNamespace(conn=None), RUN, runs_dir, url)


def test_traces_url_is_the_profile_key_else_none() -> None:
    assert cli._traces_url({"traces_url": "/t"}, "runs") == "/t"
    assert [cli._traces_url(p, "runs") for p in ({}, {"traces_url": ""}, {"traces_url": 3})] == [None] * 3


def test_one_parquet_file_lands_at_the_day_path_and_the_loose_files_are_gone(monkeypatch, tmp_path) -> None:
    pytest.importorskip("pyarrow")
    files = _stage(monkeypatch, tmp_path)

    _compact(tmp_path)

    assert [p.relative_to(tmp_path).as_posix() for p in (tmp_path / "traces").rglob("*") if p.is_file()] == [
        f"traces/2026/09/25/{RUN}.parquet"
    ]
    assert not any(f.exists() for f in files)
    assert not (tmp_path / f"{RUN}-trace").exists()
    assert store_traces.read_call(tmp_path / "traces", RUN, "call-2") == EVENTS


def test_a_rerun_after_a_crash_rewrites_the_file_without_duplicates(monkeypatch, tmp_path) -> None:
    pytest.importorskip("pyarrow")
    _stage(monkeypatch, tmp_path)
    _compact(tmp_path)
    _stage(monkeypatch, tmp_path)  # the loose files a crashed run left behind

    _compact(tmp_path)

    assert len(list(store_traces.iter_run(tmp_path / "traces", RUN))) == 2 * len(EVENTS)
    assert not (tmp_path / f"{RUN}-trace").exists()


def test_a_row_count_mismatch_keeps_every_loose_file(monkeypatch, tmp_path, capsys) -> None:
    pytest.importorskip("pyarrow")
    files = _stage(monkeypatch, tmp_path)
    monkeypatch.setattr(store_traces, "write_run", lambda *a, **k: 1)

    _compact(tmp_path)

    assert all(f.exists() for f in files)
    assert "traces: not compacted, runP wrote 1 rows for 6 events" in capsys.readouterr().err


def test_without_pyarrow_the_files_stay_and_one_warning_is_logged(monkeypatch, tmp_path, capsys) -> None:
    files = _stage(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "have_pyarrow", lambda: False)

    _compact(tmp_path)

    assert all(f.exists() for f in files)
    assert capsys.readouterr().err.count("traces: not compacted") == 1
    assert not (tmp_path / "traces").exists()


def test_a_profile_traces_url_of_a_local_directory_is_honoured(monkeypatch, tmp_path) -> None:
    pytest.importorskip("pyarrow")
    files = _stage(monkeypatch, tmp_path)
    elsewhere = tmp_path / "elsewhere"

    _compact(tmp_path, cli._traces_url({"traces_url": str(elsewhere)}, tmp_path))

    assert (elsewhere / "2026" / "09" / "25" / f"{RUN}.parquet").is_file()
    assert not (tmp_path / "traces").exists()
    assert not any(f.exists() for f in files)


def test_a_bad_traces_url_is_logged_redacted_and_keeps_the_files(monkeypatch, tmp_path, capsys) -> None:
    pytest.importorskip("pyarrow")
    files = _stage(monkeypatch, tmp_path)

    _compact(tmp_path, "s3://key:sekrit@bucket/p")

    err = capsys.readouterr().err
    assert all(f.exists() for f in files)
    assert "traces: could not compact runP" in err
    assert "sekrit" not in err
