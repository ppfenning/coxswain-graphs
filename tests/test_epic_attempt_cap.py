"""The attempt cap counts attempts on the ticket's current body, so a rewritten ticket builds again."""

from __future__ import annotations

from pathlib import Path

from core import workstore

from graphs.ops import triage_quarantine
from tests.test_epic_driver import (  # noqa: F401 -- cart and repo are fixtures
    SPECS,
    TriageAttemptRunner,
    builds_per_task,
    cart,
    drive,
    initiative,
    is_ancestor,
    new_file_patch,
    repo,
)

TRIAGE_SPECS = {**SPECS, "triage-quarantine": triage_quarantine.SPEC}
DIAGNOSIS = "the earlier attempts hit check failed: bad output"


def _amend_cart(base: dict) -> dict:
    ticket_amend = {"risk": "low", "ramp": "eligible", "apply_arm": "work_state_arm"}
    return {**base, "write_kinds": {**base["write_kinds"], "ticket_amend": ticket_amend}}


def _store_with_two_recorded_attempts(tmp_path: Path) -> tuple[Path, Path]:
    """A work store whose one task carries two attempts stamped by `record_attempt`."""
    wi = tmp_path / "wi"
    (wi / "p1-foundations").mkdir(parents=True)
    (wi / "initiative.md").write_text("---\nid: demo-initiative\ntitle: demo\n---\n\nmake the join measurable\n")
    path = wi / "p1-foundations" / "t1-probe.md"
    path.write_text(
        "---\nid: t1-probe\nphase: p1-foundations\nstate: ready\nneeds: []\nsurfaces: []\n"
        "title: schema probe\n---\n\nread the vendor schema\n"
    )
    for n in (1, 2):
        workstore.record_attempt(
            path, run=f"epic-prior-{n}", phase="p1-foundations", reason="check failed: bad output",
            ts=f"2026-09-0{n}T00:00:00+00:00",
        )
    return wi, path


def test_two_attempts_recorded_before_a_rewrite_do_not_cap_the_task_and_it_builds(repo, cart, tmp_path) -> None:  # noqa: F811
    wi, path = _store_with_two_recorded_attempts(tmp_path)
    workstore.write_item({**workstore.read_item(path), "body": "read the vendor schema and list its columns"}, path)
    result, runner = drive(repo, cart, tmp_path, work=workstore.read_initiative(wi))

    assert not any(q["id"] == "t1-probe" for q in result["quarantined"])
    assert not any(call["role"] == "triage" for call in runner.calls)
    assert builds_per_task(runner)["t1-probe"] == 1
    assert is_ancestor(repo, "epic/demo-initiative/p1-foundations--t1-probe", "epic/demo-initiative/p1-foundations")


def test_two_attempts_recorded_on_the_current_body_cap_the_task_and_it_goes_to_triage(repo, cart, tmp_path) -> None:  # noqa: F811
    wi, _ = _store_with_two_recorded_attempts(tmp_path)
    runner = TriageAttemptRunner({})
    drive(repo, _amend_cart(cart), tmp_path, runner=runner, work=workstore.read_initiative(wi), specs=TRIAGE_SPECS)

    assert any(call["role"] == "triage" for call in runner.calls)
    assert "t1-probe" not in builds_per_task(runner)


def test_triage_sees_its_prior_entry_on_an_older_body_and_escalates(repo, cart, tmp_path) -> None:  # noqa: F811
    work = initiative(two_phases=False)
    task = next(item for item in work["items"] if item["id"] == "t1-probe")
    current = workstore.body_sha(task["body"])
    task["attempts"] = [
        {"run": "epic-prior-0", "phase": "p1-foundations", "reason": "check failed: bad output",
         "body_sha": "sha-of-the-body-before-the-amend", "ts": "2026-08-31T00:00:00+00:00",
         "triage": {"class": "ticket_defect", "diagnosis": DIAGNOSIS, "run": "epic-prior-0:p1-foundations"}},
        *[
            {"run": f"epic-prior-{n}", "phase": "p1-foundations", "reason": "check failed: bad output",
             "body_sha": current, "ts": f"2026-09-0{n}T00:00:00+00:00"}
            for n in (1, 2)
        ],
    ]
    runner = TriageAttemptRunner({"t2-bench": new_file_patch("t2-bench.txt")})
    result, _ = drive(repo, _amend_cart(cart), tmp_path, runner=runner, work=work, specs=TRIAGE_SPECS)

    entry = next(q for q in result["quarantined"] if q["id"] == "t1-probe")
    assert "triage repeats a prior" in entry["reason"]
    assert "epic-prior-0" in entry["reason"]
    assert not any(p["target"] == "t1-probe" for p in result["proposals"])
