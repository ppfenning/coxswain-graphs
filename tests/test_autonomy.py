"""split_by_policy reads its streak rows from the store when one is given."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from core import ledger

from harness import split_by_policy
from harness.store_read import ledger_streak_rows
from harness.store_write import Store

REPO = Path(__file__).resolve().parent.parent
SHA = "sha-fixture"
PROFILE = "anthropic-default"
PROPOSAL = {"kind": "draft_pr_create", "risk": "low", "target": "TICKET-1"}


def entry(i: int, outcome: str = "clean", *, sha: str = SHA, profile: str = PROFILE, ts: str = "2026-08-30T00:00:00Z"):
    return {
        "run_id": f"r{i}-{sha}-{profile}-{ts}",
        "ts": ts,
        "principal": "lifecycle-propose",
        "kind": "draft_pr_create",
        "risk": "low",
        "outcome": outcome,
        "cartridge_sha": sha,
        "provider_profile": profile,
    }


@pytest.fixture
def cart(cartridge) -> dict:
    cartridge["policy"] = {"graduation_n": 3, "regraduation_multiplier": 2, "caps": {}}
    return cartridge


@pytest.fixture
def store(store_conn) -> Store:
    return Store(store_conn)


@pytest.fixture
def files(tmp_path: Path) -> tuple[Path, Path]:
    """One file ledger holding a clean streak, one holding nothing."""
    clean, empty = tmp_path / "clean.jsonl", tmp_path / "empty.jsonl"
    ledger.append([entry(i) for i in range(3)], clean)
    ledger.append([], empty)
    return clean, empty


def split(cart, path, store=None):
    return split_by_policy([PROPOSAL], cartridge=cart, ledger_path=path, provider_profile=PROFILE, store=store)


def test_without_a_store_the_split_follows_the_file(cart, files) -> None:
    clean, empty = files
    assert [len(part) for part in split(cart, clean)] == [1, 0]
    assert [len(part) for part in split(cart, empty)] == [0, 1]


def test_with_a_store_both_files_get_the_split_the_store_earns(cart, files, store) -> None:
    clean, empty = files
    for i in range(3):
        store.record_ledger(entry(i))
    assert split(cart, clean, store) == split(cart, empty, store)
    assert [len(part) for part in split(cart, empty, store)] == [1, 0]


def test_with_a_store_holding_no_streak_a_clean_file_is_not_read(cart, files, store) -> None:
    clean, _ = files
    assert [len(part) for part in split(cart, clean, store)] == [0, 1]


def test_the_store_read_returns_only_the_asked_configuration_in_epoch_ts_order(store) -> None:
    store.record_ledger(entry(2, ts="b"), epoch=2)
    store.record_ledger(entry(1, ts="a"), epoch=1)
    store.record_ledger(entry(3, ts="c"), epoch=2)
    store.record_ledger(entry(4, sha="other"), epoch=1)
    store.record_ledger(entry(5, profile="other"), epoch=1)
    rows = ledger_streak_rows(store.conn, SHA, PROFILE)
    assert [r["ts"] for r in rows] == ["a", "b", "c"]
    assert {(r["cartridge_sha"], r["provider_profile"]) for r in rows} == {(SHA, PROFILE)}


def test_importing_autonomy_does_not_load_store_traces():
    """An eager store_read import here made `python -m harness.store_traces` warn."""
    code = "import sys, harness.autonomy; print('harness.store_traces' in sys.modules)"
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=REPO)
    assert (done.stdout.strip(), done.stderr) == ("False", "")
