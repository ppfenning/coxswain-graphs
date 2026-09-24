from runner.decision_log import CallDecision, from_row, to_row

REQUIRED = {
    "role": "plan",
    "requested_tier": "standard",
    "chosen_tier": "cheap",
    "model_id": "anthropic/claude-cheap",
    "reason": "node cap",
    "ticket_key": "T-1",
    "outcome_key": "o-1",
}

SYSTEM_ONE_FIELDS = (
    "system_one_backend",
    "system_one_mode",
    "system_one_answer",
    "system_one_confidence",
    "system_one_threshold",
    "system_one_agreed",
)


def test_all_six_system_one_fields_round_trip_to_an_equal_record():
    record = CallDecision(
        **REQUIRED,
        system_one_backend="onnx",
        system_one_mode="shadow",
        system_one_answer="yes",
        system_one_confidence=0.93,
        system_one_threshold=0.9,
        system_one_agreed=True,
    )
    assert from_row(to_row(record)) == record


def test_an_old_shaped_row_loads_with_every_system_one_field_none():
    old_row = {
        **REQUIRED,
        "router_tier": "deep",
        "router_reason": "hard task",
        "claude_code_version": "2.1.0",
        "effort": "high",
        "budget_usd": 0.5,
        "clipped_by": "node_budget_cap",
    }
    decision = from_row(old_row)
    assert [getattr(decision, name) for name in SYSTEM_ONE_FIELDS] == [None] * 6
