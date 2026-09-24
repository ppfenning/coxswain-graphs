import pytest

from runner.system_one import Answer, Noul, RoleSpec
from runner.system_one_specs import role_specs

SPEC = role_specs()["handoff"]
PLAN = "{'steps': ['add x']}"
FACTS = "{'source_lines': 12, 'test_lines': 30}"
PROMPT = (
    "The build step is done.\n\n"
    f"Task: t\nPlan: {PLAN}\nSummary: did it\n"
    f"Files: ['a.py']\nChange facts: {FACTS}\n"
    "The facts listed under Change facts are already measured.\n"
)


def _answer(yes: float) -> Answer:
    return Answer("noul", "yes" if yes >= 0.5 else "no", {"yes": yes, "no": 1 - yes}, max(yes, 1 - yes))


def test_build_returns_a_noul_with_plan_and_change_facts_verbatim():
    question, state = SPEC.build({"prompt": PROMPT})
    assert isinstance(question, Noul)
    assert state == {"plan": PLAN, "change_facts": FACTS}


def test_criteria_ask_nothing_that_needs_arithmetic():
    question, _ = SPEC.build({"prompt": PROMPT})
    words = set(question.criteria.lower().replace(".", " ").replace("?", " ").replace(",", " ").split())
    assert not {"total", "count", "lines", "date"} & words


def test_build_refuses_a_prompt_without_the_sections():
    with pytest.raises(ValueError):
        SPEC.build({"prompt": "no sections here"})


def test_render_yes_point_nine_is_complete():
    assert SPEC.render(_answer(0.9)) == {"complete": True}


def test_render_yes_point_one_is_not_complete():
    assert SPEC.render(_answer(0.1)) == {"complete": False}


def test_render_at_the_boundary_is_complete():
    assert SPEC.render(_answer(0.5)) == {"complete": True}


def test_agrees_on_a_true_pair():
    assert SPEC.agrees(_answer(0.9), {"complete": True}) is True


def test_agrees_is_false_on_a_disagreeing_pair():
    assert SPEC.agrees(_answer(0.1), {"complete": True}) is False


def test_agrees_on_a_false_pair():
    assert SPEC.agrees(_answer(0.1), {"complete": False}) is True


def test_role_specs_holds_only_the_handoff_role():
    specs = role_specs()
    assert set(specs) == {"handoff"}
    assert isinstance(specs["handoff"], RoleSpec)
