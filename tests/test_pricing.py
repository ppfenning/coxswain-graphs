from runner.pricing import max_output_tokens_for_budget, price_call

_PRICES = {"gpt-priced": {"input": 3.0, "output": 15.0}, "local-model": {"input": 0, "output": 0}}


def test_price_call_on_a_priced_model_returns_the_expected_float():
    assert price_call(_PRICES, "gpt-priced", 1_000_000, 1_000_000) == 18.0


def test_price_call_on_an_unpriced_model_returns_none():
    assert price_call(_PRICES, "unknown-model", 100, 100) is None


def test_price_call_on_a_model_priced_at_zero_returns_zero_not_none():
    assert price_call(_PRICES, "local-model", 1_000_000, 1_000_000) == 0.0


def test_max_output_tokens_for_budget_on_a_priced_model_returns_the_expected_int():
    assert max_output_tokens_for_budget(_PRICES, "gpt-priced", 1.5) == 100_000


def test_max_output_tokens_for_budget_on_an_unpriced_model_returns_none():
    assert max_output_tokens_for_budget(_PRICES, "unknown-model", 1.5) is None


def test_max_output_tokens_for_budget_on_a_model_priced_at_zero_returns_none():
    assert max_output_tokens_for_budget(_PRICES, "local-model", 1.5) is None
