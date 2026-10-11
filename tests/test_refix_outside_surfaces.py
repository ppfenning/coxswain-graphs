from harness.epic import _permit_outside_surfaces

BODY = "Fix the parser.\n"
OUTSIDE = "configured check failed outside surfaces: tests/test_a.py, tests/test_b.py"


def test_outside_files_become_permitted_surfaces() -> None:
    body, surfaces = _permit_outside_surfaces(BODY, ["src/parser.py"], OUTSIDE)
    assert surfaces == ["src/parser.py", "tests/test_a.py", "tests/test_b.py"]
    assert "- tests/test_a.py" in body
    assert "- tests/test_b.py" in body
    assert body.startswith(BODY)


def test_existing_surfaces_kept_in_order() -> None:
    _, surfaces = _permit_outside_surfaces(BODY, ["z.py", "a.py"], OUTSIDE)
    assert surfaces[:2] == ["z.py", "a.py"]


def test_duplicate_file_appears_once() -> None:
    body, surfaces = _permit_outside_surfaces(BODY, ["tests/test_a.py"], OUTSIDE)
    assert surfaces == ["tests/test_a.py", "tests/test_b.py"]
    assert body.count("- tests/test_a.py") == 1


def test_repeated_file_in_reason_appears_once() -> None:
    reason = "configured check failed outside surfaces: tests/test_a.py, tests/test_a.py"
    _, surfaces = _permit_outside_surfaces(BODY, [], reason)
    assert surfaces == ["tests/test_a.py"]


def test_plain_reason_leaves_body_unchanged() -> None:
    body, surfaces = _permit_outside_surfaces(BODY, ["src/parser.py"], "configured check failed")
    assert body == BODY
    assert surfaces == ["src/parser.py"]


def test_arguments_are_not_mutated() -> None:
    original = ["src/parser.py"]
    _permit_outside_surfaces(BODY, original, OUTSIDE)
    assert original == ["src/parser.py"]
