from runner.decision_log import CallDecision, to_row
from runner.system_one_report import report


def row(*, role="plan", mode="shadow", conf=0.9, agreed=True, answer="approve", key="o"):
    return to_row(
        CallDecision(
            role=role,
            requested_tier="standard",
            chosen_tier="cheap",
            model_id="m",
            reason="r",
            ticket_key="T-1",
            outcome_key=key,
            system_one_mode=mode,
            system_one_answer=answer,
            system_one_confidence=conf,
            system_one_agreed=agreed,
        )
    )


def shadow_rows(total, agreeing, conf=0.9):
    return [row(agreed=i < agreeing, conf=conf, key=f"o{i}") for i in range(total)]


def plan(rows, outcomes=None, threshold=0.8):
    return report(rows, outcomes, {"plan": threshold})["plan"]


def test_19_of_20_agreeing_is_0_95_and_not_eligible_under_100_rows():
    r = plan(shadow_rows(20, 19))
    assert (r.agreement_rate, r.at_threshold, r.eligible_for_on) == (0.95, 20, False)


def test_100_rows_at_95_agreeing_is_eligible():
    r = plan(shadow_rows(100, 95))
    assert (r.agreement_rate, r.eligible_for_on) == (0.95, True)


def test_99_rows_at_95_agreeing_is_not_eligible():
    assert plan(shadow_rows(99, 95)).eligible_for_on is False


def test_100_rows_at_94_agreeing_is_not_eligible():
    assert plan(shadow_rows(100, 94)).eligible_for_on is False


def test_a_row_below_threshold_is_excluded_from_agreement_but_counted_in_the_total():
    rows = [row(conf=0.9, agreed=True, key="a"), row(conf=0.5, agreed=False, key="b")]
    r = plan(rows)
    assert (r.shadow_calls, r.at_threshold, r.agreement_rate, r.coverage) == (2, 1, 1.0, 0.5)


def test_rows_with_none_agreed_or_confidence_are_skipped_and_counted_nowhere_else():
    rows = [row(key="a"), row(agreed=None, key="b"), row(conf=None, key="c")]
    r = plan(rows)
    assert (r.skipped, r.shadow_calls, r.at_threshold) == (2, 1, 1)


def test_no_rows_gives_no_rate_and_no_coverage():
    r = plan([])
    assert (r.agreement_rate, r.coverage, r.eligible_for_on) == (None, None, False)


def test_an_on_mode_approve_is_flagged_when_quarantined_and_not_when_landed():
    rows = [
        row(mode="on", answer="approve", key="q"),
        row(mode="on", answer="true", key="q2"),
        row(mode="on", answer="approve", key="l"),
        row(mode="on", answer="reject", key="qr"),
        row(mode="shadow", answer="approve", key="qs"),
    ]
    outcomes = {
        "q": "quarantined",
        "q2": "quarantined",
        "l": "landed",
        "qr": "quarantined",
        "qs": "quarantined",
    }
    assert plan(rows, outcomes).flagged_approves == ("q", "q2")


def test_with_no_outcomes_nothing_is_flagged():
    assert plan([row(mode="on", key="q")], None).flagged_approves == ()


def test_each_role_uses_its_own_threshold_and_unlisted_roles_are_dropped():
    rows = [
        row(role="plan", conf=0.7, key="a"),
        row(role="review", conf=0.7, key="b"),
        row(role="other", key="c"),
    ]
    out = report(rows, None, {"plan": 0.6, "review": 0.8})
    assert sorted(out) == ["plan", "review"]
    assert (out["plan"].at_threshold, out["review"].at_threshold) == (1, 0)


def test_a_row_exactly_at_the_threshold_counts_as_at_threshold():
    r = plan([row(conf=0.8, agreed=True, key="a")], threshold=0.8)
    assert (r.at_threshold, r.agreement_rate) == (1, 1.0)


def test_answer_matching_is_exact_lowercase_so_other_casings_are_not_flagged():
    rows = [row(mode="on", answer=a, key=a) for a in ("Approve", "True", "yes", "approve")]
    outcomes = dict.fromkeys(("Approve", "True", "yes", "approve"), "quarantined")
    assert plan(rows, outcomes).flagged_approves == ("approve",)
