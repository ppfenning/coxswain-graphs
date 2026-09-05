"""The trust loop, closed: the policy asked at the right grain, the detector heard.

Two halves of one argument, and both were previously unreachable from a unit
test on either side of the seam.

The substrate ships an entry-scoped policy — `subject`, `subject_new`,
`attempts` — fully implemented and fully unit tested. The harness never passed
any of it, so the entry-level trust the design note describes was dead code seen
from the caller. That is clause 1 of the graph contract's failure list, and no
test of `autonomy_policy` can catch it, because the function is perfectly
correct; it is simply never asked the question.

The other half: `verify` already reports `trap_held`, and the harness threw the
verdict away once proposals were emitted. An entry demonstrated wrong in use
kept its clean streak until a human happened to refuse something.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from core import ledger

from harness import split_by_policy
from harness.cli import _default_ledger, _observe_trap_failures

SHA = "sha-fixture"
PROFILE = "anthropic-default"
GRAPH = "triage-propose"

ENTRY = "rb-04"
OTHER = "rb-09"


@pytest.fixture
def cart() -> dict:
    """A cartridge whose `doc_update` may actually graduate.

    Built here rather than in conftest: the shared fixture is the taxonomy the
    other suites are written against, and this one needs `doc_update` eligible
    to have anything to say about entry-level streaks.
    """
    return {
        "team": "acme",
        "cartridge_sha": SHA,
        "context": [],
        "skills": {},
        "write_kinds": {
            "doc_update": {"risk": "low", "ramp": "eligible"},
            "comment_add": {"risk": "low", "ramp": "gated"},
        },
        "policy": {"graduation_n": 3, "regraduation_multiplier": 2, "caps": {}},
    }


def proposal(**over) -> dict:
    return {
        "kind": "doc_update",
        "risk": "low",
        "target": ENTRY,
        "evidence": [{"check": "verify", "output": "ok"}],
        "rationale": "r",
        "suggested_action": "a",
        **over,
    }


def seed(path: Path, outcome: str, n: int, *, subject=None, attempts=None, kind="doc_update") -> None:
    """Rows written straight at the ledger — the same shape `record_run` writes."""
    ledger.append(
        [
            {
                "run_id": f"r-{outcome}-{subject}-{i}",
                "ts": "2026-08-30T00:00:00Z",
                "principal": GRAPH,
                "kind": kind,
                "risk": "low",
                "outcome": outcome,
                "cartridge_sha": SHA,
                "provider_profile": PROFILE,
                # Absent means absent. A written default here would be an
                # invented track record, which is what policy.py refuses to read.
                **({"subject": subject} if subject is not None else {}),
                **({"attempts": attempts} if attempts is not None else {}),
            }
            for i in range(n)
        ],
        path,
    )


def split(proposals, cart, path):
    return split_by_policy(proposals, cartridge=cart, ledger_path=path, provider_profile=PROFILE)


def triage_result(*, trap_held, verified=True, entry=ENTRY, run_id="run-1") -> dict:
    """A triage result shaped exactly as `triage_propose.run` returns one."""
    item: dict = {
        "alert": {"id": "a0"},
        "classification": {"symptom_key": "late_landing", "runbook_entry": entry, "confidence": "high"},
        "verified": verified,
    }
    if verified:
        item["verification"] = {
            "checks": [{"check": "object listing", "output": "0 objects", "supports_symptom": True}],
            "trap_considered": "SUCCESS is not evidence the file landed",
            "runbook_correction": "",
            "conclusion": "c",
            "suggested_action": "s",
            "actionable": True,
            **({} if trap_held is None else {"trap_held": trap_held}),
        }
    return {"run_id": run_id, "date": "2026-08-30", "triaged": [item], "proposals": []}


def observe(result, cart, path) -> int:
    return _observe_trap_failures(
        result,
        graph_name=GRAPH,
        ts="2026-08-31T00:00:00Z",
        cartridge=cart,
        provider_profile=PROFILE,
        ledger_path=path,
    )


# ── the policy is asked at the grain the proposal names ────────────────────────


def test_a_subjects_streak_graduates_only_that_subject(cart, tmp_path) -> None:
    """Without the wire, rb-04's history graduates rb-09 too — the average hides the bad entry."""
    path = tmp_path / "l.jsonl"
    seed(path, "clean", 3, subject=ENTRY)

    auto, gated = split([proposal(subject=ENTRY)], cart, path)
    assert len(auto) == 1 and gated == [], "the entry that earned it goes auto"

    auto, gated = split([proposal(subject=OTHER)], cart, path)
    assert auto == [] and len(gated) == 1, "an entry with no history of its own has no streak"


def test_an_entry_with_a_reversal_does_not_drag_down_its_neighbours(cart, tmp_path) -> None:
    """The other direction of the same failure: one bad entry poisoning a good one."""
    path = tmp_path / "l.jsonl"
    seed(path, "clean", 3, subject=ENTRY)
    seed(path, "reversal", 1, subject=OTHER)

    auto, _ = split([proposal(subject=ENTRY)], cart, path)
    assert len(auto) == 1, "rb-09's reversal is not rb-04's reversal"


def test_a_subjectless_proposal_falls_back_to_the_kind(cart, tmp_path) -> None:
    """Absence must stay absence, so the kind-level reading is exercised by real absence."""
    path = tmp_path / "l.jsonl"
    seed(path, "clean", 3, subject=ENTRY)

    auto, gated = split([proposal()], cart, path)
    assert len(auto) == 1 and gated == [], "kind-level counts every row, whatever subject it carries"


def test_the_kind_level_fallback_is_the_strict_reading(cart, tmp_path) -> None:
    """A subject-less caller wears every entry's reversals. That is the correct direction of error."""
    path = tmp_path / "l.jsonl"
    seed(path, "clean", 3, subject=ENTRY)
    seed(path, "reversal", 1, subject=OTHER)

    auto, gated = split([proposal()], cart, path)
    assert auto == [] and len(gated) == 1

    auto, _ = split([proposal(subject=ENTRY)], cart, path)
    assert len(auto) == 1, "and the named entry is unaffected by it"


def test_subject_new_gates_even_when_the_kind_has_graduated(cart, tmp_path) -> None:
    """Creating the entry is the one act no history can vouch for."""
    path = tmp_path / "l.jsonl"
    seed(path, "clean", 5, subject=ENTRY)
    seed(path, "clean", 5)  # and the kind itself, subject-free, is graduated too

    auto, gated = split([proposal(subject="new-symptom", subject_new=True)], cart, path)
    assert auto == [] and len(gated) == 1


def test_subject_new_false_is_not_forwarded_as_truthy(cart, tmp_path) -> None:
    """`subject_new: False` must behave as the ordinary amendment it is."""
    path = tmp_path / "l.jsonl"
    seed(path, "clean", 3, subject=ENTRY)
    auto, _ = split([proposal(subject=ENTRY, subject_new=False)], cart, path)
    assert len(auto) == 1


def test_caps_are_still_counted_per_kind_not_per_subject(cart, tmp_path) -> None:
    """Caps bound blast radius, which is a fact about the run, not about an entry."""
    path = tmp_path / "l.jsonl"
    seed(path, "clean", 3, subject=ENTRY)
    seed(path, "clean", 3, subject=OTHER)
    cart["policy"]["caps"] = {"doc_update": 2}

    auto, gated = split(
        [proposal(subject=ENTRY), proposal(subject=OTHER), proposal(subject=ENTRY)], cart, path
    )
    assert len(auto) == 2, "the cap is the kind's, so two different entries share it"
    assert len(gated) == 1, "the overflow is proposed, not dropped"


# ── attempts is ledger-side, and the harness reads the substrate's semantics ───


def test_a_clean_streak_that_took_three_tries_does_not_graduate(cart, tmp_path) -> None:
    """The fix loop must not launder struggle into trust — end to end, through the split."""
    path = tmp_path / "l.jsonl"
    seed(path, "clean", 5, subject=ENTRY, attempts=3)

    auto, gated = split([proposal(subject=ENTRY)], cart, path)
    assert auto == [] and len(gated) == 1


def test_a_proposal_carrying_attempts_still_splits_normally(cart, tmp_path) -> None:
    """`attempts` describes the row the gate will write, not the question asked now."""
    path = tmp_path / "l.jsonl"
    seed(path, "clean", 3, subject=ENTRY)

    auto, gated = split([proposal(subject=ENTRY, attempts=3)], cart, path)
    assert len(auto) == 1 and gated == [], "attempts on the proposal is not a policy input"


# ── verify's verdict reaches the ledger ────────────────────────────────────────


def test_a_trap_that_did_not_hold_is_recorded_against_the_entry(cart, tmp_path) -> None:
    """The signal `verify` already produced and the harness used to throw away."""
    path = tmp_path / "l.jsonl"
    assert observe(triage_result(trap_held=False), cart, path) == 1

    (row,) = ledger.read(path)
    assert row["outcome"] == "failure", "append_observation makes it a failure by construction"
    assert row["subject"] == ENTRY, "the entry is the thing that lost standing"
    assert row["kind"] == "doc_update"
    assert row["risk"] == "low", "risk is read off the taxonomy, never invented"
    assert row["principal"] == GRAPH, "the principal is the graph, never a person"
    assert (row["cartridge_sha"], row["provider_profile"]) == (SHA, PROFILE)
    assert row["ts"] == "2026-08-31T00:00:00Z", "the manifest's clock, not a second one"


def test_a_trap_that_held_records_nothing(cart, tmp_path) -> None:
    path = tmp_path / "l.jsonl"
    assert observe(triage_result(trap_held=True), cart, path) == 0
    assert ledger.read(path) == ()


def test_a_missing_trap_verdict_is_not_evidence(cart, tmp_path) -> None:
    """`is False` exactly: demoting on silence punishes a run that did not answer."""
    path = tmp_path / "l.jsonl"
    assert observe(triage_result(trap_held=None), cart, path) == 0
    assert ledger.read(path) == ()


def test_a_gap_with_no_matched_entry_records_nothing(cart, tmp_path) -> None:
    """The subject_new case: there is no streak to demote, and none to invent."""
    path = tmp_path / "l.jsonl"
    assert observe(triage_result(trap_held=False, entry=""), cart, path) == 0
    assert ledger.read(path) == ()


def test_an_unverified_item_never_observes(cart, tmp_path) -> None:
    """Deferred for capacity means nobody checked; there is no verdict to file."""
    path = tmp_path / "l.jsonl"
    assert observe(triage_result(trap_held=False, verified=False), cart, path) == 0
    assert ledger.read(path) == ()


def test_a_result_with_no_triaged_key_is_left_alone(cart, tmp_path) -> None:
    """Every other graph runs through the same tail and must be untouched by it."""
    path = tmp_path / "l.jsonl"
    assert observe({"run_id": "r", "proposals": []}, cart, path) == 0
    assert ledger.read(path) == ()


def test_the_demotion_is_live_not_decorative(cart, tmp_path) -> None:
    """The round trip: an entry that had graduated gates on its own next run."""
    path = tmp_path / "l.jsonl"
    seed(path, "clean", 3, subject=ENTRY)
    seed(path, "clean", 3, subject=OTHER)

    auto, _ = split([proposal(subject=ENTRY)], cart, path)
    assert len(auto) == 1, "precondition: rb-04 had earned it"

    assert observe(triage_result(trap_held=False), cart, path) == 1

    auto, gated = split([proposal(subject=ENTRY)], cart, path)
    assert auto == [] and len(gated) == 1, "the observation reset the entry's streak"

    auto, _ = split([proposal(subject=OTHER)], cart, path)
    assert len(auto) == 1, "and only that entry's — rb-09 still stands"


def test_regraduation_doubles_the_bar_for_the_demoted_entry(cart, tmp_path) -> None:
    """Slow to earn, fast to lose, expensive to earn back — at the entry's grain."""
    path = tmp_path / "l.jsonl"
    seed(path, "clean", 3, subject=ENTRY)
    observe(triage_result(trap_held=False), cart, path)
    seed(path, "clean", 3, subject=ENTRY)

    auto, gated = split([proposal(subject=ENTRY)], cart, path)
    assert auto == [] and len(gated) == 1, "3 more cleans no longer clear a bar of 6"

    seed(path, "clean", 3, subject=ENTRY)
    auto, _ = split([proposal(subject=ENTRY)], cart, path)
    assert len(auto) == 1, "6 does"


def test_risk_is_never_invented_when_the_taxonomy_is_silent(cart, tmp_path, capsys) -> None:
    """A row with a made-up risk would be counted against the wrong bar forever."""
    path = tmp_path / "l.jsonl"
    del cart["write_kinds"]["doc_update"]
    assert observe(triage_result(trap_held=False), cart, path) == 0
    assert ledger.read(path) == ()
    assert "never invented" in capsys.readouterr().err


def test_the_observation_announces_itself(cart, tmp_path, capsys) -> None:
    """A demotion nobody is told about is a rule that will be found out by surprise."""
    path = tmp_path / "l.jsonl"
    observe(triage_result(trap_held=False), cart, path)
    assert f"observation: trap did not hold for '{ENTRY}' — recorded against its streak" in capsys.readouterr().out


# ── the ledger lives outside the tree it governs ───────────────────────────────


def test_default_ledger_honours_xdg_state_home(monkeypatch, tmp_path) -> None:
    """Read at call time, so the environment is still something you can set."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert _default_ledger() == tmp_path / "state" / "agent-graphs" / "ledger.jsonl"


def test_default_ledger_falls_back_to_local_state(monkeypatch) -> None:
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    assert _default_ledger() == Path.home() / ".local" / "state" / "agent-graphs" / "ledger.jsonl"


def test_an_empty_xdg_state_home_is_not_a_path(monkeypatch) -> None:
    """`XDG_STATE_HOME=` would otherwise put the ledger at a relative path in the cwd."""
    monkeypatch.setenv("XDG_STATE_HOME", "")
    assert _default_ledger().is_absolute()


def test_the_default_ledger_is_not_inside_the_repo(monkeypatch) -> None:
    """The whole point: no patch this system applies can reach its own trust record."""
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    repo_root = Path(__file__).resolve().parent.parent
    assert not _default_ledger().resolve().is_relative_to(repo_root)
