from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from runner import anthropic_runner as anthropic_runner_module
from runner.anthropic_runner import AnthropicRunner
from runner.protocol import BudgetStop, LimitStop, RunnerError

# Prices are USD per million tokens. 1000 in + 2000 out on m-priced is 0.003 + 0.03 = 0.033.
PROFILE = {
    "tiers": {"cheap": "m-cheap", "standard": "m-priced", "deep": "m-deep"},
    "prices": {"m-priced": {"input": 3.0, "output": 15.0}},
}


class _StatusError(Exception):
    """Any SDK error that carries an HTTP status_code."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"http {status_code}")


class _Stub:
    def __init__(self, *, input_tokens: int = 1000, output_tokens: int = 2000, raises: Exception | None = None, text: str = '{"ok": true}', model: str = "m-priced") -> None:
        self.calls: list[dict[str, Any]] = []
        self._raises = raises
        self._response = SimpleNamespace(
            model=model,
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=text)],
            usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
        )
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return self._response


class _RecordingStore:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def record_call(self, call: dict[str, Any], decision: Any = None, *, run_id: str, seq: int, phase_id: str | None = None) -> int:
        self.calls.append(call)
        return 1


def _runner(stub: _Stub, store: _RecordingStore | None = None) -> AnthropicRunner:
    return AnthropicRunner(PROFILE, client=stub, store=store, run_id="r1:p1")


def _run(runner: AnthropicRunner, **kwargs: Any) -> Any:
    return runner.run(role="r", tier=kwargs.pop("tier", "standard"), schema={}, prompt="p", **kwargs)


def test_a_priced_model_records_its_tokens_and_cost():
    store = _RecordingStore()
    _run(_runner(_Stub(), store))
    row = store.calls[0]
    assert (row["input_tokens"], row["output_tokens"]) == (1000, 2000)
    assert row["cost_usd"] == pytest.approx(0.033)


def test_an_unpriced_model_records_cost_usd_none():
    store = _RecordingStore()
    _run(_runner(_Stub(model="m-cheap"), store), tier="cheap")
    assert store.calls[0]["cost_usd"] is None


def test_cumulative_spend_past_budget_usd_raises_budget_stop_with_the_spend():
    runner = _runner(_Stub())
    _run(runner, thread="th1", budget_usd=0.05)
    with pytest.raises(BudgetStop) as excinfo:
        _run(runner, thread="th1", budget_usd=0.05)
    assert excinfo.value.spent_usd == pytest.approx(0.066)
    assert (excinfo.value.role, excinfo.value.thread) == ("r", "th1")


def test_an_over_budget_call_leaves_one_row_with_its_cost_then_raises():
    store = _RecordingStore()
    with pytest.raises(BudgetStop):
        _run(_runner(_Stub(), store), budget_usd=0.01)
    assert len(store.calls) == 1
    row = store.calls[0]
    assert (row["input_tokens"], row["output_tokens"]) == (1000, 2000)
    assert row["cost_usd"] == pytest.approx(0.033)


def test_an_invalid_json_call_still_records_its_cost_and_charges_the_thread():
    store = _RecordingStore()
    stub = _Stub(text='{"ok": tru')
    runner = _runner(stub, store)
    with pytest.raises(RunnerError, match="not valid JSON"):
        _run(runner, thread="th1", budget_usd=0.01)
    row = store.calls[0]
    assert (row["ok"], row["input_tokens"], row["output_tokens"]) == (False, 1000, 2000)
    assert row["cost_usd"] == pytest.approx(0.033)
    with pytest.raises(BudgetStop) as excinfo:
        _run(runner, thread="th1", budget_usd=0.01)
    assert excinfo.value.spent_usd == pytest.approx(0.033)
    assert len(stub.calls) == 1


def test_a_tight_budget_caps_the_requests_max_tokens():
    stub = _Stub(input_tokens=0, output_tokens=1)
    _run(_runner(stub), budget_usd=0.0001)
    assert stub.calls[0]["max_tokens"] == 6


def test_a_simulated_sdk_rate_limit_exception_raises_limit_stop(monkeypatch):
    class _RateLimitError(Exception):
        pass

    monkeypatch.setattr(anthropic_runner_module, "_limit_exception_types", lambda: (_RateLimitError,))
    with pytest.raises(LimitStop):
        _run(_runner(_Stub(raises=_RateLimitError("slow down"))))


def test_a_simulated_sdk_overloaded_exception_raises_limit_stop():
    with pytest.raises(LimitStop):
        _run(_runner(_Stub(raises=_StatusError(529))))


def test_another_sdk_exception_raises_runner_error_naming_the_role():
    with pytest.raises(RunnerError, match="node 'r'"):
        _run(_runner(_Stub(raises=RuntimeError("boom"))))
