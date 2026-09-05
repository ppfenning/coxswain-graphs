"""retro-propose: stats are computed here, and a proposal must cite a real bucket."""

from __future__ import annotations

import pytest

from graphs._contract import ContractViolation
from graphs.ops import retro_propose
from runner import ScriptedRunner


def row(kind: str, outcome: str, *, subject: str | None = None, attempts: int | None = None) -> dict:
    out = {
        "run_id": "r0",
        "ts": "2026-08-01T00:00:00Z",
        "principal": "triage-propose",
        "kind": kind,
        "risk": "low",
        "outcome": outcome,
        "cartridge_sha": "sha-fixture",
        "provider_profile": "anthropic-default",
    }
    if subject is not None:
        out["subject"] = subject
    if attempts is not None:
        out["attempts"] = attempts
    return out


# rb-04: one clean, then three reversals -> streak resets to 0, matches the
# spec's own worked example ("3 reversal, 1 clean, streak 0").
# rb-09: two clean-first-try, one clean on a THIRD attempt -> that third clean
# is transparent (attempts > 1): it counts, but does not extend the streak.
# comment_add: a bucket with no subject at all.
ROWS = [
    row("doc_update", "clean", subject="rb-04"),
    row("doc_update", "reversal", subject="rb-04"),
    row("doc_update", "reversal", subject="rb-04"),
    row("doc_update", "reversal", subject="rb-04"),
    row("doc_update", "clean", subject="rb-09"),
    row("doc_update", "clean", subject="rb-09"),
    row("doc_update", "clean", subject="rb-09", attempts=3),
    row("comment_add", "clean"),
]

RETRO_RESPONSE = {
    "observations": [
        {
            "about": "comment_add streak is healthy",
            "detail": "single clean row, streak 1",
            "cites": ["comment_add|-"],
        },
    ],
    "proposals": [
        {
            "target": "rb-04",
            "rationale": "rb-04 keeps getting reversed",
            "suggested_action": "rewrite rb-04's stated trap",
            "cites": ["doc_update|rb-04"],
            "subject": "rb-04",
        },
        {
            "target": "doc_update guidance",
            "rationale": "doc_update is trending clean overall",
            "suggested_action": "note the pattern in the runbook style guide",
            "cites": ["doc_update|rb-09"],
            "subject": "",
        },
    ],
}


@pytest.fixture
def cart(cartridge) -> dict:
    cartridge["skills"]["retro"] = "acme-skills:retro"
    cartridge["write_kinds"]["doc_update"] = {"risk": "low", "ramp": "deferred"}
    return cartridge


def args(cart, **overrides) -> dict:
    return {"run_id": "r", "date": "2026-08-31", "cartridge": cart, "ledger_rows": ROWS, **overrides}


def runner(response=RETRO_RESPONSE) -> ScriptedRunner:
    return ScriptedRunner({"retro": response})


def test_stats_buckets_and_streak_arithmetic_with_attempts_gt1_transparency(cart) -> None:
    result = retro_propose.run(args(cart), runner())

    assert result["totals"] == {"rows": 8, "buckets": 3, "proposals": 2, "deferred_overflow": 0}
    assert result["observations"] == RETRO_RESPONSE["observations"]

    rb04 = next(p for p in result["proposals"] if p["target"] == "rb-04")
    assert rb04["evidence"] == [{"check": "ledger:doc_update|rb-04", "output": "3 reversal, 1 clean, streak 0"}]

    rb09 = next(p for p in result["proposals"] if p["target"] == "doc_update guidance")
    # attempts>1's clean is counted but does not extend the streak: streak is
    # 2 (the two first-try cleans), not 3.
    assert rb09["evidence"] == [{"check": "ledger:doc_update|rb-09", "output": "0 reversal, 3 clean, streak 2"}]


def test_subject_rides_into_the_emitted_proposal(cart) -> None:
    result = retro_propose.run(args(cart), runner())
    rb04 = next(p for p in result["proposals"] if p["target"] == "rb-04")
    rb09 = next(p for p in result["proposals"] if p["target"] == "doc_update guidance")
    assert rb04["subject"] == "rb-04"
    assert "subject" not in rb09, "an empty subject rides through as absent, not as ''"


def test_retro_role_runs_at_deep_tier(cart) -> None:
    scripted = runner()
    retro_propose.run(args(cart), scripted)
    assert scripted.calls[0]["role"] == "retro"
    assert scripted.calls[0]["tier"] == "deep"


def test_zero_rows_returns_early_with_no_node_call(cart) -> None:
    scripted = runner()
    result = retro_propose.run(args(cart, ledger_rows=[]), scripted)
    assert result == {
        "run_id": "r",
        "date": "2026-08-31",
        "observations": [],
        "proposals": [],
        "totals": {"rows": 0, "buckets": 0, "proposals": 0, "deferred_overflow": 0},
    }
    assert scripted.calls == [], "a retro over nothing has nothing to learn; no node call should happen"


def test_a_team_without_the_retro_role_is_told_so(cartridge) -> None:
    with pytest.raises(ContractViolation, match="needs the optional role 'retro'"):
        retro_propose.run(args(cartridge), ScriptedRunner({}))


def test_a_fabricated_cite_is_refused(cart) -> None:
    bad = {
        "observations": [],
        "proposals": [
            {
                "target": "x",
                "rationale": "r",
                "suggested_action": "a",
                "cites": ["doc_update|not-a-real-bucket"],
                "subject": "",
            }
        ],
    }
    with pytest.raises(ContractViolation, match="fabricated citation"):
        retro_propose.run(args(cart), runner(bad))


def test_a_fabricated_observation_cite_is_also_refused(cart) -> None:
    bad = {
        "observations": [{"about": "x", "detail": "y", "cites": ["doc_update|nope"]}],
        "proposals": [],
    }
    with pytest.raises(ContractViolation, match="fabricated citation"):
        retro_propose.run(args(cart), runner(bad))


def test_a_zero_cite_proposal_is_refused(cart) -> None:
    bad = {
        "observations": [],
        "proposals": [{"target": "x", "rationale": "r", "suggested_action": "a", "cites": [], "subject": ""}],
    }
    with pytest.raises(ContractViolation, match="no cites"):
        retro_propose.run(args(cart), runner(bad))


def test_cap_and_deferred_overflow_are_counted_not_dropped(cart) -> None:
    three = {
        "observations": [],
        "proposals": [
            {"target": "a", "rationale": "r", "suggested_action": "s", "cites": ["doc_update|rb-04"], "subject": ""},
            {"target": "b", "rationale": "r", "suggested_action": "s", "cites": ["doc_update|rb-09"], "subject": ""},
            {"target": "c", "rationale": "r", "suggested_action": "s", "cites": ["comment_add|-"], "subject": ""},
        ],
    }
    result = retro_propose.run(args(cart, max_proposals=2), runner(three))
    assert len(result["proposals"]) == 2
    assert result["totals"]["proposals"] == 2
    assert result["totals"]["deferred_overflow"] == 1


def test_refuses_without_ledger_rows(cart) -> None:
    incomplete = {"run_id": "r", "date": "d", "cartridge": cart}
    with pytest.raises(ContractViolation, match="args.ledger_rows is required"):
        retro_propose.run(incomplete, ScriptedRunner({}))


def test_refuses_without_a_cartridge() -> None:
    with pytest.raises(ContractViolation, match="cartridge"):
        retro_propose.run({"run_id": "r", "date": "d", "ledger_rows": []}, ScriptedRunner({}))
