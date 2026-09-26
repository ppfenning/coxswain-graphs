import json

import pytest

from harness import store_backfill
from harness.store_dialect import json_load
from harness.store_migrate import open_store
from harness.store_recost import recost, session_id_of
from harness.store_write import Store

DAY = "2026-09-24"
T0 = "2026-09-24T00:00:00Z"


def _calls(session: str, costs: list[float], prefix: str = "c") -> list[tuple]:
    return [(f"{prefix}{i}", session, f"2026-09-24T00:0{i}:00Z", cost) for i, cost in enumerate(costs)]


def _own(calls: list[tuple]) -> dict[str, float]:
    return {call_id: cost for call_id, cost, _reported in recost(calls)}


def test_a_resumed_session_gives_each_call_the_difference_of_running_totals():
    assert _own(_calls("s", [0.268, 0.361, 0.449, 0.526])) == {"c0": 0.268, "c1": 0.093, "c2": 0.088, "c3": 0.077}


def test_a_drop_in_the_running_total_is_a_restart_and_keeps_its_reported_cost():
    assert _own(_calls("s", [1.56, 0.67])) == {"c0": 1.56, "c1": 0.67}


def test_two_sessions_are_independent():
    both = _calls("a", [0.2, 0.5], "a") + _calls("b", [0.4, 0.45], "b")
    assert _own(both) == {"a0": 0.2, "a1": 0.3, "b0": 0.4, "b1": 0.05}


def test_a_call_with_no_session_is_left_out_and_input_order_does_not_matter():
    calls = [
        ("late", "s", "2026-09-24T00:02:00Z", 0.5),
        ("none", None, "2026-09-24T00:01:00Z", 9.0),
        ("early", "s", "2026-09-24T00:01:00Z", 0.2),
    ]
    assert recost(calls) == [("early", 0.2, 0.2), ("late", 0.3, 0.5)]


def test_the_session_id_is_the_first_init_event_and_absent_without_one():
    init = {"type": "system", "subtype": "init", "session_id": "s1"}
    assert session_id_of([{"type": "assistant"}, init, {**init, "session_id": "s2"}]) == "s1"
    assert session_id_of([{"type": "assistant"}]) is None


def _events(session: str | None) -> list[dict]:
    init = {"type": "system", "subtype": "init", **({"session_id": session} if session else {})}
    return [init, {"type": "result"}]


@pytest.fixture
def world(tmp_path):
    """A SQLite store with four calls of session s1, one of s2 and one with no trace, and their Parquet traces."""
    pytest.importorskip("pyarrow")
    from harness import store_traces
    from harness.traces_url import resolve_traces_root

    url = f"sqlite:///{tmp_path / 'store.db'}"
    conn = open_store(url, T0)
    store = Store(conn)
    rows = [
        ("c0", "s1", 0.268, {"trace": "x"}),
        ("c1", "s1", 0.361, {"trace": "x", "note": "kept"}),
        ("c2", "s1", 0.449, {}),
        ("c3", "s1", 0.526, {}),
        ("d0", "s2", 1.0, {}),
        ("n0", None, 0.7, {}),
    ]
    for seq, (call_id, _session, cost, extra) in enumerate(rows):
        call = {"id": call_id, "role": "build", "ts": f"2026-09-24T00:0{seq}:00Z", "cost_usd": cost, **extra}
        store.record_call(call, run_id="r1", seq=seq)
    conn.close()
    traces = resolve_traces_root(str(tmp_path / "traces"), tmp_path, {})
    store_traces.write_run(traces, DAY, "r1", {c: _events(s) for c, s, _cost, _extra in rows if c != "n0"})
    for name in ("runs", "work"):
        (tmp_path / name).mkdir()
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("", encoding="utf-8")
    return url, tmp_path / "runs", tmp_path / "work", ledger, tmp_path / "traces"


def _stored(url: str) -> dict[str, tuple[float, dict]]:
    conn = open_store(url, T0)
    try:
        sql = "SELECT call_id, cost_usd, detail_json FROM node_calls"
        return {c: (cost, json_load(d) or {}) for c, cost, d in conn.query_all(sql)}
    finally:
        conn.close()


def _run(world, *flags: str) -> int:
    url, runs, work, ledger, traces = world
    return store_backfill.main([str(runs), str(work), str(ledger), url, "--traces-root", str(traces), *flags])


def test_recost_resumed_rewrites_costs_and_keeps_the_reported_figure_in_detail_json(world):
    assert _run(world, "--recost-resumed") == 0
    stored = _stored(world[0])
    costs = {c: v[0] for c, v in stored.items()}
    assert costs == {"c0": 0.268, "c1": 0.093, "c2": 0.088, "c3": 0.077, "d0": 1.0, "n0": 0.7}
    assert stored["c1"][1] == {"trace": "x", "note": "kept", "reported_cost_usd": 0.361}
    assert "reported_cost_usd" not in stored["c0"][1]


def test_a_second_run_changes_nothing(world):
    _run(world, "--recost-resumed")
    first = _stored(world[0])
    assert _run(world, "--recost-resumed") == 0
    assert _stored(world[0]) == first


def test_the_report_carries_the_recost_keys_only_with_the_flag(world, capsys):
    _run(world, "--recost-resumed")
    report = json.loads(capsys.readouterr().out)
    assert (report["recosted_calls"], report["recost_delta_usd"]) == (3, -1.078)
    _run(world)
    assert "recosted_calls" not in json.loads(capsys.readouterr().out)


def test_dry_run_prints_the_count_and_sums_and_writes_nothing(world, capsys):
    before = _stored(world[0])
    assert _run(world, "--recost-resumed", "--dry-run") == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed == {"would_recost_calls": 3, "cost_before_usd": 1.336, "cost_after_usd": 0.258}
    assert _stored(world[0]) == before


def test_dry_run_without_the_flag_is_refused(world):
    with pytest.raises(SystemExit):
        _run(world, "--dry-run")
