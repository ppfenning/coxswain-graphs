import json

from runner.schema_answer import extract_json, retry_prompt, schema_instruction, shape_answer, validate

SCHEMA = {
    "type": "object",
    "required": ["verdict"],
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "revise"]},
        "meta": {"type": "object", "properties": {"score": {"type": "integer"}}},
    },
}


def test_schema_instruction_ends_with_the_sorted_key_dump():
    assert schema_instruction({"b": 1, "a": 2}).endswith('{"a": 2, "b": 1}')


def test_extract_takes_a_fenced_json_block():
    assert extract_json('Here:\n```json\n{"a": 1}\n```\nDone.') == {"a": 1}


def test_extract_takes_bare_json_wrapped_in_prose():
    assert extract_json('Sure, {"a": {"b": 2}} is my answer.') == {"a": {"b": 2}}


def test_extract_skips_a_first_span_that_does_not_parse():
    assert extract_json('Use {not json} then {"ok": true}') == {"ok": True}


def test_extract_returns_none_when_there_is_no_json():
    assert extract_json("no braces here") is None


def test_validate_reports_a_missing_required_key_with_its_path():
    assert validate({}, SCHEMA) == ["$: missing required key 'verdict'"]


def test_validate_reports_a_wrong_type_at_a_nested_path():
    bad = {"verdict": "approve", "meta": {"score": "high"}}
    assert validate(bad, SCHEMA) == ["$.meta.score: expected integer, got string"]


def test_validate_reports_an_enum_miss():
    assert validate({"verdict": "maybe"}, SCHEMA) == ['$.verdict: "maybe" is not one of ["approve", "revise"]']


def test_validate_walks_array_items_and_treats_bool_as_not_integer():
    schema = {"type": "array", "items": {"type": "integer"}}
    assert validate([1, True], schema) == ["$[1]: expected integer, got boolean"]


def test_shape_answer_succeeds_on_a_valid_reply():
    assert shape_answer('```json\n{"verdict": "revise"}\n```', SCHEMA) == ({"verdict": "revise"}, [])


def test_shape_answer_fails_when_nothing_parses():
    assert shape_answer("sorry", SCHEMA) == (None, ["$: no JSON object found in the reply"])


def test_shape_answer_fails_when_validation_fails():
    assert shape_answer(json.dumps({"verdict": "maybe"}), SCHEMA) == (
        None,
        ['$.verdict: "maybe" is not one of ["approve", "revise"]'],
    )


def test_retry_prompt_carries_the_original_the_bad_reply_and_the_errors():
    out = retry_prompt("Review this.", "oops", ["$.verdict: bad"])
    assert "Review this." in out
    assert "oops" in out
    assert "$.verdict: bad" in out
