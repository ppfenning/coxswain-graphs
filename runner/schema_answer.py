"""Schema-shaped answers from plain text: extract JSON, validate it, build the retry prompt."""

from __future__ import annotations

import json
import re

_FENCE = re.compile(r"```json[ \t]*\r?\n(.*?)```", re.DOTALL | re.IGNORECASE)
_NOT_FOUND = object()

_TYPES = {
    "null": lambda v: v is None,
    "boolean": lambda v: isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "string": lambda v: isinstance(v, str),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
}


def schema_instruction(schema: dict) -> str:
    return (
        "Reply with one JSON object and nothing else: no prose before or after it. "
        "The object must match this JSON schema.\n" + json.dumps(schema, sort_keys=True)
    )


def _parse(text: str) -> object:
    try:
        return json.loads(text)
    except ValueError:
        return _NOT_FOUND


def _first_brace_span(text: str) -> object:
    decoder = json.JSONDecoder()
    for start in (i for i, c in enumerate(text) if c == "{"):
        try:
            return decoder.raw_decode(text, start)[0]
        except ValueError:
            continue
    return _NOT_FOUND


def _find(text: str) -> object:
    """The parsed value, or _NOT_FOUND. A JSON null is a found value, not a miss."""
    fenced = _FENCE.search(text)
    parsed = _parse(fenced.group(1)) if fenced else _NOT_FOUND
    return parsed if parsed is not _NOT_FOUND else _first_brace_span(text)


def extract_json(text: str) -> object | None:
    found = _find(text)
    return None if found is _NOT_FOUND else found


def _type_name(value: object) -> str:
    return next((name for name, is_type in _TYPES.items() if is_type(value)), type(value).__name__)


def _errors(value: object, schema: dict, path: str) -> list[str]:
    declared = schema.get("type")
    wanted = [declared] if isinstance(declared, str) else list(declared or [])
    type_errors = (
        [f"{path}: expected {' or '.join(wanted)}, got {_type_name(value)}"]
        if wanted and not any(_TYPES.get(t, lambda _: True)(value) for t in wanted)
        else []
    )
    enum = schema.get("enum")
    enum_errors = (
        [f"{path}: {json.dumps(value)} is not one of {json.dumps(enum)}"]
        if enum is not None and value not in enum
        else []
    )
    if type_errors or enum_errors:
        return type_errors + enum_errors
    if isinstance(value, dict):
        missing = [f"{path}: missing required key '{k}'" for k in schema.get("required", []) if k not in value]
        nested = [
            e
            for k, sub in schema.get("properties", {}).items()
            if k in value
            for e in _errors(value[k], sub, f"{path}.{k}")
        ]
        return missing + nested
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        return [e for i, item in enumerate(value) for e in _errors(item, schema["items"], f"{path}[{i}]")]
    return []


def validate(value: object, schema: dict) -> list[str]:
    """Checks type, required, properties, items and enum. Paths start at `$`."""
    return _errors(value, schema, "$")


def shape_answer(text: str, schema: dict) -> tuple[object | None, list[str]]:
    found = _find(text)
    if found is _NOT_FOUND:
        return None, ["$: no JSON object found in the reply"]
    errors = validate(found, schema)
    return (None, errors) if errors else (found, [])


def retry_prompt(original: str, bad_text: str, errors: list[str]) -> str:
    listed = "\n".join(f"- {e}" for e in errors)
    return (
        f"{original}\n\nYour previous reply was rejected.\n\nRejected reply:\n{bad_text}\n\n"
        f"Errors:\n{listed}\n\nReply again with one corrected JSON object and nothing else."
    )
