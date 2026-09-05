"""The autonomy loop, end to end through the harness.

These exist because the policy module was fully implemented and fully unit
tested while the shell never once called it — every proposal went to the gate
forever and no kind could graduate. Unit tests on a pure function cannot catch
a caller that never calls it, so these drive the seam itself.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from core import ledger

from harness import apply_arm_for as _apply_arm_for
from harness import auto_apply as _auto_apply
from harness import split_by_policy as _split_by_policy

SHA = "sha-fixture"
PROFILE = "anthropic-default"

PROPOSAL = {
    "kind": "draft_pr_create",
    "risk": "low",
    "target": "TICKET-1",
    "evidence": [{"check": "tests", "output": "ok"}],
    "rationale": "r",
    "suggested_action": "a",
}


@pytest.fixture
def cart(cartridge) -> dict:
    cartridge["policy"] = {"graduation_n": 3, "regraduation_multiplier": 2, "caps": {}}
    cartridge["write_kinds"]["draft_pr_create"]["apply_arm"] = "work_item_arm"
    cartridge["skills"]["work_item_arm"] = "acme-skills:create-ticket"
    return cartridge


def seed(path: Path, outcome: str, n: int, kind: str = "draft_pr_create") -> None:
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


def split(proposals, cart, path):
    return _split_by_policy(proposals, cartridge=cart, ledger_path=path, provider_profile=PROFILE)


def test_day_one_everything_is_gated(cart, tmp_path) -> None:
    auto, gated = split([PROPOSAL], cart, tmp_path / "l.jsonl")
    assert (auto, len(gated)) == ([], 1)


def test_a_kind_actually_graduates_through_the_shell(cart, tmp_path) -> None:
    """The end-to-end fact that was previously unreachable."""
    path = tmp_path / "l.jsonl"
    seed(path, "clean", 3)
    auto, gated = split([PROPOSAL], cart, path)
    assert len(auto) == 1 and gated == []


def test_one_reversal_sends_it_back_to_the_gate(cart, tmp_path) -> None:
    path = tmp_path / "l.jsonl"
    seed(path, "clean", 3)
    seed(path, "reversal", 1)
    auto, gated = split([PROPOSAL], cart, path)
    assert auto == [] and len(gated) == 1


def test_rows_from_another_config_do_not_count(cart, tmp_path) -> None:
    """A streak earned under a different cartridge is not this cartridge's streak."""
    path = tmp_path / "l.jsonl"
    seed(path, "clean", 5)
    cart["cartridge_sha"] = "a-different-sha"
    auto, gated = split([PROPOSAL], cart, path)
    assert auto == [], "changing the cartridge must reset autonomy"
    assert len(gated) == 1


def test_the_caller_filters_so_policy_is_never_handed_mixed_rows(cart, tmp_path) -> None:
    """policy raises on mixed rows; the shell must not be what triggers that."""
    path = tmp_path / "l.jsonl"
    seed(path, "clean", 3)
    ledger.append(
        [
            {
                "run_id": "other",
                "ts": "t",
                "principal": "p",
                "kind": "draft_pr_create",
                "risk": "low",
                "outcome": "clean",
                "cartridge_sha": "some-other-sha",
                "provider_profile": "some-other-profile",
            }
        ],
        path,
    )
    auto, _ = split([PROPOSAL], cart, path)  # must not raise
    assert len(auto) == 1


def test_caps_bound_a_graduated_kind_within_one_run(cart, tmp_path) -> None:
    path = tmp_path / "l.jsonl"
    seed(path, "clean", 3)
    cart["policy"]["caps"] = {"draft_pr_create": 2}
    auto, gated = split([PROPOSAL] * 5, cart, path)
    assert len(auto) == 2, "the cap bounds one run"
    assert len(gated) == 3, "the overflow is proposed, not dropped"


def test_auto_apply_goes_through_the_arm_the_cartridge_names(cart) -> None:
    class Arm:
        def __init__(self):
            self.calls = []

        def run(self, **kwargs):
            self.calls.append(kwargs)
            return {"applied": True, "detail": "created"}

    arm = Arm()
    ok, detail = _auto_apply(PROPOSAL, cartridge=cart, runner=arm)
    assert ok and detail == "created"
    assert arm.calls[0]["role"] == "work_item_arm", "the arm is a role, resolved by the cartridge"


def test_a_pr_armed_kind_is_never_reported_as_applied(cart) -> None:
    cart["write_kinds"]["draft_pr_create"]["apply_arm"] = "pr"
    ok, detail = _auto_apply(PROPOSAL, cartridge=cart, runner=None)
    assert not ok and "no executable apply arm" in detail


def test_apply_arm_lookup_reads_the_cartridge(cart) -> None:
    assert _apply_arm_for("draft_pr_create", cart) == "work_item_arm"
    assert _apply_arm_for("merge", cart) is None
