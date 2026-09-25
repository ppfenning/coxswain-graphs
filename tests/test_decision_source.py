from runner.decision_log import RouterDecision
from runner.decision_source import DecisionSource, NoDecisionSource, ask
from runner.tier_resolution import Hints

DECISION = RouterDecision(
    chosen_class="reason",
    model="m",
    effort="low",
    budget_usd=0.5,
    reasons=(),
    clipped_by=(),
)


def test_no_decision_source_returns_none():
    assert NoDecisionSource()("plan", Hints()) is None


def test_no_decision_source_is_a_decision_source():
    assert isinstance(NoDecisionSource(), DecisionSource)


def test_ask_returns_the_source_value():
    assert ask(lambda role, hints: DECISION, "plan", None) is DECISION


def test_ask_swallows_a_raising_source():
    def boom(role, hints):
        raise RuntimeError("router down")

    assert ask(boom, "plan", Hints()) is None


def test_ask_passes_role_and_hints_through_unchanged():
    seen = []

    def record(role, hints):
        seen.append((role, hints))

    hints = Hints(judgment="high")
    ask(record, "build", hints)
    assert seen == [("build", hints)]
