import pytest

from graphs.delivery.decompose_size import (
    SizeError,
    SizeVerdict,
    apply_override,
    mark_new_surfaces,
    parse_size_verdict,
    single_ticket_initiative,
    ticket_slug,
)

MODEL_TASK = SizeVerdict("task", "One file changes.", ("src/a.py",), "model")
INTAKE = {"id": "Add_Widget", "title": "Add widget", "body": "Do it.", "checks": ["pytest -q"]}


def test_a_valid_task_output_parses_with_its_surfaces():
    raw = {"verdict": "task", "reason": "One file changes.", "surfaces": ["src/a.py"]}
    assert parse_size_verdict(raw) == MODEL_TASK


def test_a_valid_epic_output_parses_with_no_surfaces():
    raw = {"verdict": "epic", "reason": "Three repos move together."}
    assert parse_size_verdict(raw) == SizeVerdict("epic", "Three repos move together.", (), "model")


def test_a_reason_with_an_abbreviation_is_still_one_sentence():
    raw = {"verdict": "epic", "reason": "Several modules move, e.g. the parser and Dr. Who's loader."}
    assert parse_size_verdict(raw).source == "model"


@pytest.mark.parametrize(
    "raw, message",
    [
        pytest.param(None, "size output is not an object", id="not-a-dict"),
        pytest.param(
            {"verdict": "story", "reason": "x."}, "verdict must be task or epic, got 'story'", id="bad-verdict"
        ),
        pytest.param({"verdict": "epic", "reason": "  "}, "reason must be a non-empty string", id="blank-reason"),
        pytest.param({"verdict": "epic", "reason": "One. Two."}, "reason must be one sentence", id="two-sentences"),
        pytest.param(
            {"verdict": "task", "reason": "One file.", "surfaces": "src/a.py"},
            "a task verdict needs surfaces as a list of paths",
            id="surfaces-not-a-list",
        ),
        pytest.param(
            {"verdict": "task", "reason": "One file.", "surfaces": []},
            "a task verdict needs at least one surface",
            id="task-empty-surfaces",
        ),
    ],
)
def test_a_malformed_output_returns_its_error_and_never_raises(raw, message):
    assert parse_size_verdict(raw) == SizeError(message)


def test_an_epic_override_replaces_the_model_verdict_and_drops_its_surfaces():
    assert apply_override(MODEL_TASK, {"size": "epic"}) == SizeVerdict(
        "epic", "intake frontmatter sets size: epic", (), "override"
    )


def test_a_task_override_with_no_model_surfaces_keeps_them_empty():
    epic = SizeVerdict("epic", "Big.", (), "model")
    assert apply_override(epic, {"size": "task"}) == SizeVerdict(
        "task", "intake frontmatter sets size: task", (), "override"
    )


def test_a_bad_override_value_returns_an_error():
    assert apply_override(MODEL_TASK, {"size": "huge"}) == SizeError("size override must be task or epic, got 'huge'")


def test_no_size_key_passes_the_model_verdict_through():
    assert apply_override(MODEL_TASK, {"title": "x"}) == MODEL_TASK


def test_surfaces_missing_from_the_repo_are_marked_new_and_the_input_is_kept():
    surfaces = ["src/a.py", "src/b.py"]
    assert mark_new_surfaces(surfaces, {"src/a.py"}) == ["src/a.py", "src/b.py (new)"]
    assert surfaces == ["src/a.py", "src/b.py"]


def test_a_surface_already_marked_new_is_not_marked_twice():
    assert mark_new_surfaces(["x.py (new)"], set()) == ["x.py (new)"]


def test_a_directory_surface_exists_when_a_repo_file_sits_under_it():
    repo = {"src/ui/snapshots/a.snap", "src/a.py"}
    assert mark_new_surfaces(["src/ui/snapshots/", "docs/", "./src/a.py"], repo) == [
        "src/ui/snapshots/",
        "docs/ (new)",
        "./src/a.py",
    ]


def test_the_ticket_slug_is_lowercase_and_dash_separated():
    assert ticket_slug("Add_Widget v2!") == "add-widget-v2"


def test_a_single_ticket_initiative_has_one_phase_and_one_ticket():
    verdict = SizeVerdict("task", "One file.", ("src/a.py", "src/b.py"), "model")
    assert single_ticket_initiative(INTAKE, verdict, {"src/a.py"}) == {
        "phases": [{"id": "p1", "goal": "Add widget"}],
        "tasks": [
            {
                "id": "add-widget",
                "phase": "p1",
                "title": "Add widget",
                "body": "Do it.",
                "needs": [],
                "surfaces": ["src/a.py", "src/b.py (new)"],
                "checks": ["pytest -q"],
            }
        ],
        "rationale": "One file.",
    }


def test_an_intake_id_with_nothing_to_slug_returns_an_error():
    intake = {**INTAKE, "id": "!!!"}
    assert single_ticket_initiative(intake, MODEL_TASK, set()) == SizeError(
        "intake id '!!!' has no letters or digits to slug"
    )


def test_an_intake_missing_title_and_body_returns_an_error():
    assert single_ticket_initiative({"id": "x"}, MODEL_TASK, set()) == SizeError("intake is missing title, body")


def test_an_epic_verdict_cannot_become_a_single_ticket():
    epic = SizeVerdict("epic", "Big.", (), "model")
    assert single_ticket_initiative(INTAKE, epic, set()) == SizeError("a single-ticket initiative needs a task verdict")


def test_a_task_override_with_no_surfaces_cannot_become_a_ticket():
    bare = SizeVerdict("task", "intake frontmatter sets size: task", (), "override")
    assert single_ticket_initiative(INTAKE, bare, set()) == SizeError(
        "a task verdict with no surfaces cannot become a ticket"
    )
