import json

from harness.trace_columns import COLUMNS, events_of, to_rows


def _rows(events):
    return to_rows("r", "c", "2026-09-25", events)


def test_columns_are_plain_data_with_seq_int32_and_the_rest_string():
    assert COLUMNS == (
        ("run_id", "string"),
        ("call_id", "string"),
        ("seq", "int32"),
        ("day", "string"),
        ("type", "string"),
        ("subtype", "string"),
        ("tool", "string"),
        ("event", "string"),
    )


def test_tool_is_the_name_of_the_first_tool_use_block_of_an_assistant_event():
    event = {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "text", "text": "hi"},
                {"type": "tool_use", "name": "Bash"},
                {"type": "tool_use", "name": "Read"},
            ]
        },
    }
    assert _rows([event])[0]["tool"] == "Bash"


def test_tool_is_none_for_a_user_event_even_with_a_tool_use_block():
    event = {"type": "user", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}}
    assert _rows([event])[0]["tool"] is None


def test_tool_is_none_when_content_is_a_string():
    event = {"type": "assistant", "message": {"content": "hello"}}
    assert _rows([event])[0]["tool"] is None


def test_missing_subtype_and_type_give_none():
    row = _rows([{"type": "assistant"}, {"other": 1}])
    assert (row[0]["type"], row[0]["subtype"]) == ("assistant", None)
    assert (row[1]["type"], row[1]["subtype"]) == (None, None)


def test_present_type_and_subtype_are_copied():
    row = _rows([{"type": "system", "subtype": "init"}])[0]
    assert (row["type"], row["subtype"]) == ("system", "init")


def test_events_of_returns_the_input_from_rows_in_any_order():
    events = [
        {"z": 1, "a": "café ☃", "type": "user"},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}},
    ]
    rows = _rows(events)
    assert all(json.loads(r["event"]) == e for r, e in zip(rows, events))
    assert events_of(list(reversed(rows))) == events


def test_event_text_keeps_key_order_and_non_ascii():
    text = _rows([{"z": 1, "a": "é"}])[0]["event"]
    assert text == '{"z": 1, "a": "é"}'


def test_seq_starts_at_zero_and_is_an_int():
    rows = _rows([{"type": "a"}, {"type": "b"}])
    assert [r["seq"] for r in rows] == [0, 1]
    assert all(type(r["seq"]) is int for r in rows)


def test_rows_have_exactly_the_column_keys_and_pass_ids_and_day_through():
    row = _rows([{"type": "a"}])[0]
    assert list(row) == [name for name, _ in COLUMNS]
    assert (row["run_id"], row["call_id"], row["day"]) == ("r", "c", "2026-09-25")
