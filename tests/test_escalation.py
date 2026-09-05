"""Escalation: the harness decides what a change IS, from the diff's paths.

`_change_facts` counts a diff instead of asking the builder how big it was, and
`harness/checks.py` runs the tests instead of asking a reviewer whether they
pass. These tests hold `harness/escalate.py` to the same rule one level up: what
KIND of change this is — ordinary work, or a change to the rules the work is
judged by — is derived from the patch, not accepted from the graph that emitted
the proposal.

The last test is the one the module exists for. Everything above it can be
right while the system is still wrong, because a correct classifier that runs
after the policy has already cleared a proposal is a report, not a gate. So it
seeds a real streak, escalates, and puts the result through the real
`split_by_policy` — and asserts the un-escalated twin would have gone auto, so
that the gating is provably the escalation's doing and not the seeding's.

Plain dicts throughout. No network, no worktree, no runner.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from core import ledger

from harness import split_by_policy
from harness.escalate import escalate_self_modification, governance_hits, touched_paths

SHA = "sha-fixture"
PROFILE = "anthropic-default"
LEDGER_NAME = "ledger.jsonl"
ELSEWHERE = f"/x/y/{LEDGER_NAME}"

GOVERNANCE_PATCH = """\
diff --git a/harness/gate.py b/harness/gate.py
--- a/harness/gate.py
+++ b/harness/gate.py
@@ -1,3 +1,3 @@
-    return PROPOSE
+    return AUTO
"""

ORDINARY_PATCH = """\
diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1,3 +1,3 @@
-old
+new
"""


def build_proposal(**over) -> dict:
    """A patch-bearing proposal, in the shape `graphs/_contract.proposal` emits."""
    return {
        "kind": "draft_pr_create",
        "risk": "low",
        "target": "TICKET-1",
        "evidence": [{"check": "checks:pytest", "output": "pass — 12 passed (exit 0)"}],
        "rationale": "the plan was carried out",
        "suggested_action": "open a draft PR",
        **over,
    }


@pytest.fixture
def cart(cartridge) -> dict:
    """The fixture cartridge, taught the kind the base taxonomy already carries.

    `self_modification` is `ramp: never` here for the same reason it is in
    `cartridges/base/cartridge.yaml`: escalation is only a gate if the kind it
    escalates to cannot itself graduate.
    """
    cartridge["policy"] = {"graduation_n": 3, "regraduation_multiplier": 2, "caps": {}}
    cartridge["write_kinds"]["draft_pr_create"] = {
        "risk": "low",
        "ramp": "eligible",
        "apply_arm": "work_item_arm",
    }
    cartridge["write_kinds"]["self_modification"] = {"risk": "high", "ramp": "never", "apply_arm": "pr"}
    cartridge["write_kinds"]["item_create"] = {"risk": "low", "ramp": "gated"}
    return cartridge


def seed(path: Path, outcome: str, n: int, kind: str = "draft_pr_create") -> None:
    """Clean rows for one kind under one configuration. Same shape the shell writes."""
    ledger.append(
        [
            {
                "run_id": f"r{i}",
                "ts": "2026-08-30T00:00:00Z",
                "principal": "lifecycle-propose",
                "kind": kind,
                "risk": "low",
                "outcome": outcome,
                "cartridge_sha": SHA,
                "provider_profile": PROFILE,
            }
            for i in range(n)
        ],
        path,
    )


# ── touched_paths: mechanical, and deliberately over-collecting ──────────────


def test_a_plain_modification_reports_the_file_once() -> None:
    patch = "--- a/src/a.py\n+++ b/src/a.py\n-old\n+new\n"
    assert touched_paths(patch) == ["src/a.py"], "both sides name one file, deduplicated"


def test_an_add_reports_only_the_new_side() -> None:
    patch = "diff --git a/harness/new.py b/harness/new.py\n--- /dev/null\n+++ b/harness/new.py\n+first\n"
    assert touched_paths(patch) == ["harness/new.py"], "/dev/null is not a path"


def test_a_delete_reports_only_the_old_side() -> None:
    patch = "--- a/core/gone.py\n+++ /dev/null\n-last\n"
    assert touched_paths(patch) == ["core/gone.py"]


def test_a_rename_is_seen_through_the_diff_git_line() -> None:
    """A pure rename carries no ---/+++ pair at all; only the git header names it."""
    patch = (
        "diff --git a/core/policy.py b/core/rules.py\n"
        "similarity index 100%\n"
        "rename from core/policy.py\n"
        "rename to core/rules.py\n"
    )
    assert touched_paths(patch) == ["core/policy.py", "core/rules.py"]


def test_a_mode_only_change_still_names_its_file() -> None:
    patch = "diff --git a/harness/gate.py b/harness/gate.py\nold mode 100644\nnew mode 100755\n"
    assert touched_paths(patch) == ["harness/gate.py"]


def test_many_files_come_back_deduplicated_and_sorted() -> None:
    patch = (
        "diff --git a/z/last.py b/z/last.py\n--- a/z/last.py\n+++ b/z/last.py\n+x\n"
        "diff --git a/m/mid.py b/m/mid.py\n--- a/m/mid.py\n+++ b/m/mid.py\n+y\n"
        "diff --git a/z/last.py b/z/last.py\n--- a/z/last.py\n+++ b/z/last.py\n+again\n"
    )
    assert touched_paths(patch) == ["m/mid.py", "z/last.py"], (
        "one entry per file however many headers name it, in a stable order"
    )


def test_a_path_with_a_space_survives_the_git_header() -> None:
    """Split on the ` b/` seam, not on whitespace — a filename with a space in it
    must not arrive as two half-paths."""
    patch = "diff --git a/harness/my file.py b/harness/my file.py\nold mode 100644\n"
    assert touched_paths(patch) == ["harness/my file.py"]


def test_an_empty_patch_touches_nothing() -> None:
    assert touched_paths("") == []


# ── governance_hits: whole segments, at any depth ────────────────────────────


def test_a_directory_entry_hits_everything_under_it() -> None:
    assert governance_hits(["cartridges/base/cartridge.yaml"], ledger_path=ELSEWHERE) == [
        "cartridges/base/cartridge.yaml"
    ]


def test_an_exact_file_entry_hits() -> None:
    assert governance_hits(["core/policy.py"], ledger_path=ELSEWHERE) == ["core/policy.py"]


def test_a_diff_rooted_a_level_up_still_hits() -> None:
    """The check must not key off the string start; a patch built against a parent
    checkout names `repo/harness/gate.py` and is the same edit."""
    assert governance_hits(["repo/harness/gate.py"], ledger_path=ELSEWHERE) == ["repo/harness/gate.py"]


def test_a_prefix_that_is_not_a_segment_does_not_hit() -> None:
    assert governance_hits(["harnessy/file.py"], ledger_path=ELSEWHERE) == [], (
        "harnessy/ is not harness/; matching must be on segment boundaries"
    )


def test_ordinary_work_hits_nothing() -> None:
    assert governance_hits(["src/app.py", "docs/notes.md"], ledger_path=ELSEWHERE) == []


def test_the_ledger_is_matched_by_basename_wherever_it_sits() -> None:
    """--ledger is configurable, so the path is not a constant to list. A patch
    writing any file by that name is editing the record or shadowing it."""
    assert governance_hits(["state/ledger.jsonl"], ledger_path=ELSEWHERE) == ["state/ledger.jsonl"]


def test_a_differently_named_ledger_does_not_drag_the_default_along() -> None:
    assert governance_hits(["state/ledger.jsonl"], ledger_path="/x/y/trust.jsonl") == []


def test_hits_come_back_sorted_and_deduplicated() -> None:
    paths = ["src/app.py", "harness/cli.py", "cartridges/local/cartridge.yaml", "harness/cli.py"]
    assert governance_hits(paths, ledger_path=ELSEWHERE) == [
        "cartridges/local/cartridge.yaml",
        "harness/cli.py",
    ]


def test_extra_entries_extend_the_list_without_replacing_it() -> None:
    hits = governance_hits(["ops/runbook.md", "harness/cli.py"], ledger_path=ELSEWHERE, extra=("ops/",))
    assert hits == ["harness/cli.py", "ops/runbook.md"]


# ── escalate_self_modification ───────────────────────────────────────────────


def test_a_governance_patch_rewrites_the_patch_bearing_proposal(cart) -> None:
    original = build_proposal()
    out, hits = escalate_self_modification(
        [original], patch=GOVERNANCE_PATCH, cartridge=cart, ledger_path=ELSEWHERE
    )

    assert hits == ["harness/gate.py"]
    (item,) = out
    assert item["kind"] == "self_modification"
    assert item["risk"] == "high", "risk is read off the taxonomy, never invented here"
    assert item["escalated_from"] == "draft_pr_create", "what it claimed to be stays on the record"
    assert item["target"] == "TICKET-1" and item["rationale"] == original["rationale"]


def test_the_original_evidence_survives_and_the_paths_are_appended(cart) -> None:
    out, _ = escalate_self_modification(
        [build_proposal()], patch=GOVERNANCE_PATCH, cartridge=cart, ledger_path=ELSEWHERE
    )
    evidence = out[0]["evidence"]
    assert evidence[0] == {"check": "checks:pytest", "output": "pass — 12 passed (exit 0)"}, (
        "the check arm's verdict is still true about this change"
    )
    assert evidence[-1] == {"check": "governance_paths", "output": "harness/gate.py"}


def test_the_escalation_does_not_mutate_what_it_was_handed(cart) -> None:
    original = build_proposal()
    escalate_self_modification([original], patch=GOVERNANCE_PATCH, cartridge=cart, ledger_path=ELSEWHERE)
    assert original["kind"] == "draft_pr_create" and len(original["evidence"]) == 1


def test_kinds_that_do_not_carry_the_patch_pass_through(cart) -> None:
    """A scoping item_create emitted by the same run is not the change."""
    scoping = {
        "kind": "item_create",
        "risk": "low",
        "target": "EPIC-9",
        "evidence": [{"check": "scope", "output": "1 epic"}],
        "rationale": "attach to the epic",
        "suggested_action": "create it",
    }
    out, hits = escalate_self_modification(
        [scoping, build_proposal()], patch=GOVERNANCE_PATCH, cartridge=cart, ledger_path=ELSEWHERE
    )
    assert hits
    assert out[0] == scoping, "untouched, in the same escalated run"
    assert out[1]["kind"] == "self_modification"


def test_an_ordinary_patch_changes_nothing(cart) -> None:
    proposals = [build_proposal(), build_proposal(target="TICKET-2")]
    before = [dict(p) for p in proposals]
    out, hits = escalate_self_modification(
        proposals, patch=ORDINARY_PATCH, cartridge=cart, ledger_path=ELSEWHERE
    )
    assert hits == []
    assert out == before, "a clean patch must be free; nothing to route around"


def test_a_cartridge_that_cannot_name_the_kind_refuses_loudly(cart) -> None:
    """Not a fallback to some nearby high-risk kind, and not a synthesised one —
    a taxonomy that cannot name this kind cannot express the ramp that makes
    the escalation mean anything."""
    del cart["write_kinds"]["self_modification"]
    with pytest.raises(ValueError, match="self_modification"):
        escalate_self_modification(
            [build_proposal()], patch=GOVERNANCE_PATCH, cartridge=cart, ledger_path=ELSEWHERE
        )


def test_a_kind_declared_without_a_risk_refuses_too(cart) -> None:
    cart["write_kinds"]["self_modification"] = {"ramp": "never"}
    with pytest.raises(ValueError, match="risk"):
        escalate_self_modification(
            [build_proposal()], patch=GOVERNANCE_PATCH, cartridge=cart, ledger_path=ELSEWHERE
        )


def test_a_patch_touching_the_run_s_own_ledger_escalates(cart, tmp_path) -> None:
    path = tmp_path / LEDGER_NAME
    patch = f"--- a/{LEDGER_NAME}\n+++ b/{LEDGER_NAME}\n+{{\"outcome\": \"clean\"}}\n"
    out, hits = escalate_self_modification(
        [build_proposal()], patch=patch, cartridge=cart, ledger_path=path
    )
    assert hits == [LEDGER_NAME]
    assert out[0]["kind"] == "self_modification"


# ── the fact this module exists for ──────────────────────────────────────────


def test_an_earned_streak_does_not_carry_a_governance_edit_past_the_gate(cart, tmp_path) -> None:
    """The whole argument, end to end through the real policy split.

    `draft_pr_create` has graduated here — three clean rows under this exact
    cartridge and profile — so an ordinary change of that kind auto-applies with
    no human in the loop. The change in front of us edits `harness/gate.py`. The
    graph called it a `draft_pr_create`, because from inside the graph that is
    exactly what it is.
    """
    path = tmp_path / LEDGER_NAME
    seed(path, "clean", 3)
    proposals = [build_proposal()]

    twin_auto, twin_gated = split_by_policy(
        proposals, cartridge=cart, ledger_path=path, provider_profile=PROFILE
    )
    assert len(twin_auto) == 1 and twin_gated == [], (
        "the control: un-escalated, this proposal writes on its streak alone"
    )

    escalated, hits = escalate_self_modification(
        proposals, patch=GOVERNANCE_PATCH, cartridge=cart, ledger_path=path
    )
    assert hits == ["harness/gate.py"]

    auto, gated = split_by_policy(
        escalated, cartridge=cart, ledger_path=path, provider_profile=PROFILE
    )
    assert auto == [], "no streak on a mundane kind may carry an edit to the rules"
    assert len(gated) == 1 and gated[0]["kind"] == "self_modification"
    assert gated[0]["escalated_from"] == "draft_pr_create"


def test_the_escalated_kind_cannot_itself_graduate(cart, tmp_path) -> None:
    """ramp: never, so there is no history that unlocks it. Escalation is one-way
    by construction rather than by our remembering to make it so."""
    path = tmp_path / LEDGER_NAME
    seed(path, "clean", 50, kind="self_modification")
    escalated, _ = escalate_self_modification(
        [build_proposal()], patch=GOVERNANCE_PATCH, cartridge=cart, ledger_path=path
    )
    auto, gated = split_by_policy(
        escalated, cartridge=cart, ledger_path=path, provider_profile=PROFILE
    )
    assert auto == [] and len(gated) == 1
