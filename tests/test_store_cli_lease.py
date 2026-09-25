import pytest

from harness.store_cli_lease import lease_acquire, lease_release, lease_renew

T0 = "2026-09-24T00:00:00Z"
T10 = "2026-09-24T00:00:10Z"
NAME = "chair"
KEYS = {"ok", "epoch", "holder"}


@pytest.fixture
def conn(store_conn):
    return store_conn


def test_acquire_on_a_free_name_returns_epoch_one_and_the_holder(conn):
    assert lease_acquire(conn, NAME, "a", T0, 30) == {"ok": True, "epoch": 1, "holder": "a"}


def test_every_result_has_exactly_the_three_keys(conn):
    results = [
        lease_acquire(conn, NAME, "a", T0, 30),
        lease_renew(conn, NAME, "a", 1, T10, 30),
        lease_release(conn, NAME, "a", 1),
    ]
    assert [set(r) for r in results] == [KEYS, KEYS, KEYS]


def test_renew_with_the_right_epoch_is_ok(conn):
    lease_acquire(conn, NAME, "a", T0, 30)
    assert lease_renew(conn, NAME, "a", 1, T10, 30) == {"ok": True, "epoch": None, "holder": None}


def test_renew_with_a_stale_epoch_is_refused(conn):
    lease_acquire(conn, NAME, "a", T0, 30)
    assert lease_renew(conn, NAME, "a", 0, T10, 30) == {"ok": False, "epoch": None, "holder": None}


def test_release_with_the_right_epoch_is_ok(conn):
    lease_acquire(conn, NAME, "a", T0, 30)
    assert lease_release(conn, NAME, "a", 1) == {"ok": True, "epoch": None, "holder": None}


def test_release_with_a_wrong_holder_is_refused(conn):
    lease_acquire(conn, NAME, "a", T0, 30)
    assert lease_release(conn, NAME, "b", 1) == {"ok": False, "epoch": None, "holder": None}


def test_acquire_against_a_live_lease_held_by_another_is_refused(conn):
    lease_acquire(conn, NAME, "a", T0, 30)
    assert lease_acquire(conn, NAME, "b", T10, 30) == {"ok": False, "epoch": 1, "holder": "a"}
