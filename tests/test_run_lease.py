"""A run's lease name, and the heartbeat that keeps the lease alive on its own connection."""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from harness import run_lease
from harness.run_lease import Heartbeat, lease_name
from harness.store_dialect import default_url
from harness.store_lease import acquire, release
from harness.store_migrate import open_store


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _heartbeat_at(conn, name: str) -> str:
    return conn.query_one("SELECT heartbeat_at FROM leases WHERE name = ?", (name,))[0]


def _wait_until(condition, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


@pytest.mark.parametrize(
    ("run_id", "expected"),
    [
        ("x-12", "runs:x"),
        ("x-y-3", "runs:x-y"),
        ("cos", "runs:cos"),
        ("go-cli-strangler-2", "runs:go-cli-strangler"),
    ],
)
def test_lease_name_drops_only_a_trailing_numeric_suffix(run_id: str, expected: str) -> None:
    assert lease_name(run_id) == expected


def test_a_tick_advances_heartbeat_at_and_stop_joins_the_thread(tmp_path) -> None:
    url = default_url(tmp_path)
    conn = open_store(url, _now())
    name = lease_name("x-1")
    # Stamped to the second, so acquire a minute back (still inside the ttl) for a tick to show.
    result = acquire(conn, name, "x-1", (datetime.now(UTC) - timedelta(seconds=60)).isoformat(), 120)
    before = _heartbeat_at(conn, name)
    beat = Heartbeat(url, name, "x-1", result.epoch, clock=_now, interval=0.05)

    beat.start()
    advanced = _wait_until(lambda: _heartbeat_at(conn, name) != before)
    beat.stop()

    assert advanced
    assert not beat.alive
    conn.close()


def test_a_stale_epoch_warns_once_and_ends_the_thread_without_raising(tmp_path, capsys) -> None:
    url = default_url(tmp_path)
    conn = open_store(url, _now())
    name = lease_name("x-1")
    result = acquire(conn, name, "x-1", _now(), 120)
    release(conn, name, "x-1", result.epoch)
    beat = Heartbeat(url, name, "x-1", result.epoch, clock=_now, interval=0.05)

    beat.start()
    ended = _wait_until(lambda: not beat.alive)
    beat.stop()

    assert ended
    assert capsys.readouterr().err.splitlines() == [f"lease: heartbeat for {name} stopped: stale epoch {result.epoch}"]
    conn.close()


def test_a_store_that_cannot_open_warns_once_and_ends_the_thread(tmp_path, capsys) -> None:
    beat = Heartbeat(f"sqlite:///{tmp_path}/missing/dir/cox.db", "runs:x", "x-1", 1, clock=_now, interval=0.05)

    beat.start()
    ended = _wait_until(lambda: not beat.alive)

    assert ended
    [line] = capsys.readouterr().err.splitlines()
    assert line.startswith("lease: heartbeat for runs:x stopped: cannot open the store:")


def test_a_renew_that_raises_warns_once_and_ends_the_thread(monkeypatch, tmp_path, capsys) -> None:
    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    url = default_url(tmp_path)
    open_store(url, _now()).close()
    monkeypatch.setattr(run_lease, "renew", broken)
    beat = Heartbeat(url, "runs:x", "x-1", 1, clock=_now, interval=0.05)

    beat.start()
    ended = _wait_until(lambda: not beat.alive)

    assert ended
    assert capsys.readouterr().err.splitlines() == ["lease: heartbeat for runs:x stopped: disk I/O error"]


def test_stop_returns_within_its_timeout_when_a_renew_is_stuck(monkeypatch, tmp_path, capsys) -> None:
    entered, gate = threading.Event(), threading.Event()

    def stuck(*args, **kwargs):
        entered.set()
        gate.wait(5)
        return True

    url = default_url(tmp_path)
    open_store(url, _now()).close()
    monkeypatch.setattr(run_lease, "renew", stuck)
    beat = Heartbeat(url, "runs:x", "x-1", 1, clock=_now, interval=0.01)
    beat.start()
    assert entered.wait(5)

    began = time.monotonic()
    beat.stop(timeout=0.05)
    waited = time.monotonic() - began
    gate.set()

    assert waited < 1.0
    assert "still in a store call after 0.05s" in capsys.readouterr().err
    assert _wait_until(lambda: not beat.alive)


def test_stop_is_safe_before_start_and_twice() -> None:
    beat = Heartbeat("sqlite:///unused.db", "runs:x", "x-1", 1, clock=_now)

    beat.stop()
    beat.stop()

    assert not beat.alive
