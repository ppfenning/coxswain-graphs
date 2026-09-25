import sys
from pathlib import Path

import pytest

from harness import store_traces as st

INIT = {"type": "system", "subtype": "init", "model": "claude-haiku-4-5-20251001", "tools": ["StructuredOutput"]}


def test_day_dir_and_run_file_are_nested_year_month_day():
    assert st.day_dir(Path("/r"), "2026-09-24") == Path("/r/2026/09/24")
    assert st.run_file(Path("/r"), "2026-09-24", "run-1") == Path("/r/2026/09/24/run-1.jsonl.zst")


def test_to_lines_is_deterministic_for_a_literal_input():
    expected = [
        '{"call_id":"c1","event":{"a":1},"run_id":"r1","seq":0}',
        '{"call_id":"c1","event":{"b":2},"run_id":"r1","seq":1}',
    ]
    assert st.to_lines("r1", "c1", [{"a": 1}, {"b": 2}]) == expected
    assert st.to_lines("r1", "c1", [{"a": 1}, {"b": 2}]) == expected


def test_two_calls_in_one_run_file_read_back_separately_in_order(tmp_path):
    first = [INIT, {"type": "assistant", "n": 1}, {"type": "result", "n": 2}]
    second = [{"type": "assistant", "n": 3}, {"type": "result", "n": 4}]
    a = st.append_call(tmp_path, "2026-09-24", "r1", "c1", first)
    b = st.append_call(tmp_path, "2026-09-24", "r1", "c2", second)
    assert a == b
    assert list(a.parent.iterdir()) == [a]
    assert st.read_call(tmp_path, "r1", "c1") == first
    assert st.read_call(tmp_path, "r1", "c2") == second


def test_calls_on_different_days_land_in_different_day_directories(tmp_path):
    a = st.append_call(tmp_path, "2026-09-24", "r1", "c1", [{"n": 1}])
    b = st.append_call(tmp_path, "2026-09-25", "r1", "c2", [{"n": 2}])
    assert a.parent != b.parent
    assert (a.parent.name, b.parent.name) == ("24", "25")
    assert st.read_call(tmp_path, "r1", "c1") == [{"n": 1}]
    assert st.read_call(tmp_path, "r1", "c2") == [{"n": 2}]
    assert [r["call_id"] for r in st.iter_run(tmp_path, "r1")] == ["c1", "c2"]


def test_an_unknown_call_or_run_reads_as_empty(tmp_path):
    st.append_call(tmp_path, "2026-09-24", "r1", "c1", [{"n": 1}])
    assert st.read_call(tmp_path, "r1", "nope") == []
    assert st.read_call(tmp_path, "other-run", "c1") == []
    assert st.read_call(tmp_path / "missing", "r1", "c1") == []


def test_without_zstandard_the_error_names_the_traces_extra(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "zstandard", None)
    with pytest.raises(st.TracesUnavailable, match="traces extra"):
        st.append_call(tmp_path, "2026-09-24", "r1", "c1", [{"n": 1}])
