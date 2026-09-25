import pytest

from harness.store_dialect import POSTGRES, SQLITE, forbidden_constructs
from harness.store_lease import (
    SQL,
    LeaseResult,
    _expiry,
    _sql,
    acquire,
    assert_epoch,
    lease_state,
    release,
    renew,
)
from harness.store_migrate import open_store

T0 = "2026-09-24T00:00:00Z"
T10 = "2026-09-24T00:00:10Z"
T40 = "2026-09-24T00:00:40Z"
T100 = "2026-09-24T00:01:40Z"
NAME = "chair"


@pytest.fixture
def conn():
    c = open_store("sqlite:///:memory:", T0)
    yield c
    c.close()


def _epoch(conn):
    row = conn.query_one("SELECT epoch FROM leases WHERE name = ?", (NAME,))
    return None if row is None else row[0]


def test_expiry_adds_ttl_seconds_in_fixed_form():
    assert _expiry(T0, 30) == "2026-09-24T00:00:30Z"


def test_expiry_normalises_an_offset_to_utc():
    assert _expiry("2026-09-24T02:00:00+02:00", 5) == "2026-09-24T00:00:05Z"


def test_lease_state_free_for_no_row():
    assert lease_state(None, T0) == "free"


def test_lease_state_held_before_expiry():
    assert lease_state((NAME, "a", 1, T0, T40), T10) == "held"


def test_lease_state_expired_at_and_after_expiry():
    row = (NAME, "a", 1, T0, T40)
    assert (lease_state(row, T40), lease_state(row, T100)) == ("expired", "expired")


def test_first_acquire_gives_epoch_one(conn):
    assert acquire(conn, NAME, "a", T0, 30) == LeaseResult(True, 1, "a")


def test_second_holder_is_refused_while_live_and_names_the_holder(conn):
    acquire(conn, NAME, "a", T0, 30)
    assert acquire(conn, NAME, "b", T10, 30) == LeaseResult(False, 1, "a")
    assert _epoch(conn) == 1


def test_takeover_after_expiry_fences_the_old_holder(conn):
    acquire(conn, NAME, "a", T0, 30)
    assert acquire(conn, NAME, "b", T40, 30) == LeaseResult(True, 2, "b")
    assert renew(conn, NAME, "a", 1, T40, 30) is False
    assert assert_epoch(conn, NAME, 1, T40) is False
    assert assert_epoch(conn, NAME, 2, T40) is True


def test_release_then_acquire_by_another_holder_gives_epoch_three(conn):
    acquire(conn, NAME, "a", T0, 30)
    acquire(conn, NAME, "b", T40, 30)
    assert release(conn, NAME, "b", 2) is True
    assert acquire(conn, NAME, "c", T40, 30) == LeaseResult(True, 3, "c")


def test_same_holder_reacquiring_while_live_increments(conn):
    acquire(conn, NAME, "a", T0, 30)
    assert acquire(conn, NAME, "a", T10, 30) == LeaseResult(True, 2, "a")
    assert assert_epoch(conn, NAME, 1, T10) is False


def test_renew_extends_expiry_when_holder_and_epoch_match(conn):
    acquire(conn, NAME, "a", T0, 30)
    assert renew(conn, NAME, "a", 1, T10, 60) is True
    assert assert_epoch(conn, NAME, 1, T40) is True


def test_renew_fails_on_wrong_holder_wrong_epoch_and_after_release(conn):
    acquire(conn, NAME, "a", T0, 30)
    assert renew(conn, NAME, "b", 1, T10, 30) is False
    assert renew(conn, NAME, "a", 2, T10, 30) is False
    release(conn, NAME, "a", 1)
    assert renew(conn, NAME, "a", 1, T10, 30) is False


def test_release_with_a_stale_epoch_or_holder_changes_nothing(conn):
    acquire(conn, NAME, "a", T0, 30)
    assert release(conn, NAME, "a", 9) is False
    assert release(conn, NAME, "b", 1) is False
    assert assert_epoch(conn, NAME, 1, T10) is True


def test_release_keeps_the_epoch(conn):
    acquire(conn, NAME, "a", T0, 30)
    release(conn, NAME, "a", 1)
    assert _epoch(conn) == 1
    assert assert_epoch(conn, NAME, 1, T0) is False


def test_assert_epoch_false_for_unknown_name_and_after_expiry(conn):
    acquire(conn, NAME, "a", T0, 30)
    assert assert_epoch(conn, "other", 1, T0) is False
    assert assert_epoch(conn, NAME, 1, T40) is False


def test_a_lost_insert_race_is_a_refusal_not_an_error(conn):
    # The row appears between the failed UPDATE and the INSERT: simulate by
    # inserting a live row held by another holder, then acquiring as someone else.
    conn.execute(
        "INSERT INTO leases (name, holder, epoch, heartbeat_at, expires_at) VALUES (?, ?, ?, ?, ?)",
        (NAME, "rival", 1, T0, T40),
    )
    assert acquire(conn, NAME, "a", T10, 30) == LeaseResult(False, 1, "rival")


def test_epoch_never_decreases_across_a_mixed_sequence(conn):
    steps = (
        lambda: acquire(conn, NAME, "a", T0, 30),
        lambda: renew(conn, NAME, "a", 1, T10, 30),
        lambda: acquire(conn, NAME, "b", T10, 30),
        lambda: acquire(conn, NAME, "b", T100, 30),
        lambda: release(conn, NAME, "a", 1),
        lambda: release(conn, NAME, "b", 2),
        lambda: acquire(conn, NAME, "a", T100, 30),
        lambda: acquire(conn, NAME, "a", T100, 30),
        lambda: renew(conn, NAME, "b", 2, T100, 30),
    )
    seen = []
    for step in steps:
        step()
        seen.append(_epoch(conn))
    assert seen == sorted(seen)
    assert seen[-1] == 4


def test_no_sql_uses_a_forbidden_construct_and_placeholders_follow_the_dialect():
    assert [forbidden_constructs(s) for s in SQL] == [()] * len(SQL)
    assert all("?" not in _sql(POSTGRES, s) for s in SQL)
    assert all("%s" not in _sql(SQLITE, s) for s in SQL)
