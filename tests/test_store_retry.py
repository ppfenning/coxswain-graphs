import pytest

from harness.store_retry import is_connection_error, retry_store_write

# A stand-in shaped like psycopg's class, so these tests run where the postgres extra is not installed.
OperationalError = type("OperationalError", (Exception,), {"__module__": "psycopg"})
IntegrityError = type("IntegrityError", (Exception,), {"__module__": "psycopg"})


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def lost() -> Exception:
    return OperationalError("the connection is lost")


def scripted(*outcomes, clock: FakeClock | None = None):
    """A callable that raises or returns each outcome in turn, recording the clock at each call."""
    starts: list[float] = []

    def call():
        outcome = outcomes[len(starts)]
        starts.append(clock() if clock else 0.0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return call, starts


def run(write, clock: FakeClock, reconnect=None, **kwargs):
    reconnects: list[int] = []
    result = retry_store_write(
        write,
        reconnect=reconnect or (lambda: reconnects.append(1)),
        clock=clock,
        sleep=clock.sleep,
        **kwargs,
    )
    return result, reconnects


def test_a_write_that_fails_twice_then_succeeds_returns_its_value_after_two_reconnects():
    clock = FakeClock()
    write, calls = scripted(lost(), lost(), "ok")
    result, reconnects = run(write, clock)
    assert (result, len(reconnects), len(calls), clock.sleeps) == ("ok", 2, 3, [1.0, 2.0])


def test_a_reconnect_that_fails_while_the_server_is_down_is_retried_not_raised():
    clock = FakeClock()
    write, writes = scripted(lost(), "ok")
    refused = OperationalError('connection failed: connection to server at "db", port 5432 failed: Connection refused')
    reconnect, reconnects = scripted(refused, None)
    result, _ = run(write, clock, reconnect=reconnect)
    assert (result, len(writes), len(reconnects), clock.sleeps) == ("ok", 2, 2, [1.0, 2.0])


def test_a_write_that_fails_past_the_window_re_raises_the_last_error():
    clock = FakeClock()
    errors = [lost() for _ in range(50)]
    write, calls = scripted(*errors)
    with pytest.raises(OperationalError) as caught:
        run(write, clock, window_s=300.0)
    assert caught.value is errors[len(calls) - 1]
    assert clock.now == 1300.0


def test_no_attempt_starts_after_the_deadline_and_the_last_starts_at_it():
    clock = FakeClock()
    write, starts = scripted(*[lost() for _ in range(50)], clock=clock)
    with pytest.raises(OperationalError):
        run(write, clock, window_s=20.0, base_s=2.0)
    assert starts == [1000.0, 1002.0, 1006.0, 1014.0, 1020.0]


def test_a_non_connection_error_is_raised_on_the_first_attempt_with_no_reconnect():
    clock = FakeClock()
    boom = ValueError("bad row")
    write, calls = scripted(boom, "never")
    reconnect, reconnects = scripted(None)
    with pytest.raises(ValueError) as caught:
        run(write, clock, reconnect=reconnect)
    assert (caught.value is boom, len(calls), reconnects, clock.sleeps) == (True, 1, [], [])


def test_an_operational_error_that_is_not_a_lost_connection_is_raised_with_no_reconnect():
    clock = FakeClock()
    denied = OperationalError('connection failed: FATAL: password authentication failed for user "cox"')
    write, calls = scripted(denied, "never")
    reconnect, reconnects = scripted(None)
    with pytest.raises(OperationalError) as caught:
        run(write, clock, reconnect=reconnect)
    assert (caught.value is denied, len(calls), reconnects, clock.sleeps) == (True, 1, [], [])


def test_sleeps_double_from_the_base_and_the_last_one_is_cut_to_the_deadline():
    clock = FakeClock()
    write, _ = scripted(*[lost() for _ in range(50)])
    with pytest.raises(OperationalError):
        run(write, clock, window_s=20.0, base_s=2.0)
    assert clock.sleeps == [2.0, 4.0, 8.0, 6.0]


def test_the_connection_error_rule():
    assert is_connection_error(lost())
    assert is_connection_error(OperationalError("server closed the connection unexpectedly"))
    assert not is_connection_error(OperationalError('database "cox" does not exist'))
    assert not is_connection_error(IntegrityError("the connection is lost"))
    assert not is_connection_error(ValueError("the connection is lost"))


def test_the_real_psycopg_classes_match_the_rule():
    psycopg = pytest.importorskip("psycopg")
    assert is_connection_error(psycopg.OperationalError("the connection is lost"))
    assert is_connection_error(psycopg.errors.AdminShutdown("terminating connection due to administrator command"))
    assert not is_connection_error(psycopg.IntegrityError("the connection is lost"))
