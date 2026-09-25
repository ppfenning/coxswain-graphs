import json
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("pyarrow")

from harness import store_traces as st
from harness.trace_columns import to_rows
from harness.traces_url import resolve_traces_root

REPO = Path(__file__).resolve().parent.parent
KEYS = ["run_id", "call_id", "seq", "day", "type", "subtype", "tool", "event"]


def _row(call_id: str, seq: int) -> dict:
    return {"run_id": "r", "call_id": call_id, "seq": seq, "day": "2026-09-25", "type": "t", "subtype": None, "tool": None, "event": "{}"}


def _dump(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "harness.store_traces", *args], capture_output=True, text=True, cwd=REPO)


def test_parse_argv_accepts_only_dump_root_run_id():
    assert st.parse_argv(["dump", "r", "id"]) == ("r", "id")
    bad = ([], ["dump"], ["dump", "r"], ["nope", "r", "id"], ["dump", "r", "id", "x"])
    assert [st.parse_argv(a) for a in bad] == [None] * 5


def test_dump_lines_pin_the_keys_their_order_and_the_call_then_seq_sort():
    rows = [_row("b", 0), _row("a", 1), _row("a", 0)]
    line = '{"run_id": "r", "call_id": "%s", "seq": %d, "day": "2026-09-25", "type": "t", "subtype": null, "tool": null, "event": "{}"}'
    assert st.dump_lines(rows) == [line % ("a", 0), line % ("a", 1), line % ("b", 0)]


def test_a_written_run_dumps_as_json_lines_in_call_then_seq_order(tmp_path):
    calls = {"c2": [{"type": "result", "n": 3}], "c1": [{"type": "system", "subtype": "init"}, {"type": "assistant", "n": 1}]}
    st.write_run(resolve_traces_root(str(tmp_path), Path("."), {}), "2026-09-25", "run1", calls)
    result = _dump("dump", str(tmp_path), "run1")
    rows = [r for c, ev in calls.items() for r in to_rows("run1", c, "2026-09-25", ev)]
    expected = sorted(rows, key=lambda r: (r["call_id"], r["seq"]))
    assert result.returncode == 0
    assert [json.loads(line) for line in result.stdout.splitlines()] == expected
    assert all(list(json.loads(line)) == KEYS for line in result.stdout.splitlines())


def test_a_run_with_no_file_exits_3_with_empty_stdout(tmp_path):
    result = _dump("dump", str(tmp_path), "missing")
    assert (result.returncode, result.stdout) == (3, "")


@pytest.mark.parametrize("args", [(), ("dump",), ("dump", "somewhere"), ("nope", "somewhere", "run1")])
def test_bad_arguments_exit_2_with_one_stderr_line(args):
    result = _dump(*args)
    assert (result.returncode, result.stdout, len(result.stderr.splitlines())) == (2, "", 1)


def test_a_credentialed_url_exits_2_naming_only_the_run_id():
    result = _dump("dump", "s3://user:secret@bucket/x", "run1")
    assert result.returncode == 2
    assert "secret" not in result.stderr and "bucket" not in result.stderr
    assert "run1" in result.stderr
