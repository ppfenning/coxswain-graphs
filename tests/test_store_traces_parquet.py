import sys

import pytest

pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")

from harness import store_traces as st  # noqa: E402
from harness.trace_columns import COLUMNS  # noqa: E402
from harness.traces_url import resolve_traces_root  # noqa: E402

DAY = "2026-09-24"
FIRST = [{"type": "system", "subtype": "init"}, {"type": "assistant", "n": 1}, {"type": "result", "n": 2}]
SECOND = [{"type": "assistant", "n": 3}, {"type": "result", "n": 4}]


@pytest.fixture
def root(tmp_path):
    return resolve_traces_root(str(tmp_path), tmp_path, {})


def _file(tmp_path, run_id="r1"):
    return tmp_path / "2026" / "09" / "24" / f"{run_id}.parquet"


def test_run_parquet_nests_year_month_day_under_the_root_path():
    assert st.run_parquet("/r", "2026-09-05", "x") == "/r/2026/09/05/x.parquet"


def test_write_then_read_call_round_trips_and_returns_the_row_count(root):
    assert st.write_run(root, DAY, "r1", {"c1": FIRST}) == 3
    assert st.read_call(root, "r1", "c1") == FIRST


def test_writing_a_call_twice_replaces_it_instead_of_duplicating(root):
    st.write_run(root, DAY, "r1", {"c1": FIRST})
    assert st.write_run(root, DAY, "r1", {"c1": SECOND}) == 2
    assert st.read_call(root, "r1", "c1") == SECOND


def test_a_second_call_added_later_keeps_the_first(root):
    st.write_run(root, DAY, "r1", {"c1": FIRST})
    assert st.write_run(root, DAY, "r1", {"c2": SECOND}) == 5
    assert st.read_call(root, "r1", "c1") == FIRST
    assert st.read_call(root, "r1", "c2") == SECOND


def test_the_file_is_zstd_compressed_and_leaves_no_temp_file(root, tmp_path):
    st.write_run(root, DAY, "r1", {"c1": FIRST})
    path = _file(tmp_path)
    column = pq.ParquetFile(path).metadata.row_group(0).column(0)
    assert column.compression == "ZSTD"
    assert list(path.parent.iterdir()) == [path]


def test_columns_and_int32_seq_match_the_spec_and_rows_sort_by_call_then_seq(root, tmp_path):
    st.write_run(root, DAY, "r1", {"c2": SECOND, "c1": FIRST})
    table = pq.read_table(_file(tmp_path))
    assert [(f.name, str(f.type)) for f in table.schema] == list(COLUMNS)
    assert table.column("call_id").to_pylist() == ["c1"] * 3 + ["c2"] * 2
    assert table.column("seq").to_pylist() == [0, 1, 2, 0, 1]


def test_iter_run_yields_every_call(root):
    st.write_run(root, DAY, "r1", {"c1": FIRST, "c2": SECOND})
    assert [(r["call_id"], r["seq"]) for r in st.iter_run(root, "r1")] == [("c1", 0), ("c1", 1), ("c1", 2), ("c2", 0), ("c2", 1)]


def test_a_str_and_a_path_root_read_what_a_traces_root_wrote(root, tmp_path):
    st.write_run(root, DAY, "r1", {"c1": FIRST})
    assert st.read_call(str(tmp_path), "r1", "c1") == FIRST
    assert st.read_call(tmp_path, "r1", "c1") == FIRST


def test_an_unknown_run_or_call_or_root_reads_as_empty(root, tmp_path):
    st.write_run(root, DAY, "r1", {"c1": FIRST})
    assert st.read_call(root, "r1", "nope") == []
    assert st.read_call(root, "other", "c1") == []
    assert list(st.iter_run(tmp_path / "missing", "r1")) == []


def test_a_legacy_only_run_still_reads(root, tmp_path):
    st.append_call(tmp_path, DAY, "r1", "c1", FIRST)
    assert st.read_call(root, "r1", "c1") == FIRST
    assert st.read_call(tmp_path, "r1", "c1") == FIRST
    assert [r["call_id"] for r in st.iter_run(tmp_path, "r1")] == ["c1"] * 3


def test_read_legacy_file_reads_one_day_file_by_path(tmp_path):
    path = st.append_call(tmp_path, DAY, "r1", "c1", SECOND)
    assert [r["event"] for r in st.read_legacy_file(path)] == SECOND


def test_a_run_with_both_formats_reads_the_parquet(root, tmp_path):
    st.append_call(tmp_path, DAY, "r1", "c1", SECOND)
    st.write_run(root, DAY, "r1", {"c1": FIRST})
    assert st.read_call(root, "r1", "c1") == FIRST
    assert {r["call_id"] for r in st.iter_run(root, "r1")} == {"c1"}
    assert len(list(st.iter_run(root, "r1"))) == 3


def test_without_pyarrow_write_run_names_the_error_and_readers_fall_back(tmp_path, monkeypatch):
    local = resolve_traces_root(str(tmp_path), tmp_path, {})
    st.append_call(tmp_path, DAY, "r1", "c1", FIRST)
    monkeypatch.setitem(sys.modules, "pyarrow", None)
    with pytest.raises(st.ParquetUnavailable, match="pyarrow"):
        st.write_run(local, DAY, "r1", {"c1": SECOND})
    assert st.read_call(tmp_path, "r1", "c1") == FIRST
