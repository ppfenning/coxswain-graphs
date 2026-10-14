"""A fresh epic launch of an ordinary initiative writes card_branch; the shipping one and a resume never do."""

from __future__ import annotations

import pytest

from harness.epic import SHIPPING_INITIATIVE_ID, card_mode
from tests.test_epic_driver import (  # noqa: F401 -- cart and repo are fixtures
    cart,
    drive,
    initiative,
    repo,
)


@pytest.mark.parametrize(
    ("initiative_id", "fresh", "expected"),
    [
        ("demo-initiative", True, True),
        ("demo-initiative", False, False),
        (SHIPPING_INITIATIVE_ID, True, False),
        (SHIPPING_INITIATIVE_ID, False, False),
    ],
)
def test_card_mode_is_true_only_for_a_fresh_launch_of_a_non_shipping_initiative(initiative_id, fresh, expected) -> None:
    assert card_mode(initiative_id, fresh) is expected


def test_the_shipping_id_is_the_literal_the_tools_side_checks() -> None:
    assert SHIPPING_INITIATIVE_ID == "builds-on-one-card-branch-and-opens-one-pr-per"


def test_a_fresh_ordinary_launch_writes_card_branch_on_run_and_phases(repo, cart, tmp_path) -> None:  # noqa: F811
    result, _ = drive(repo, cart, tmp_path)
    assert result["card_branch"] is True
    assert len(result["phases"]) == 2
    assert all(p["card_branch"] is True for p in result["phases"])


def test_a_fresh_launch_of_the_shipping_initiative_writes_the_key_on_neither(repo, cart, tmp_path) -> None:  # noqa: F811
    work = {**initiative(), "id": SHIPPING_INITIATIVE_ID}
    result, _ = drive(repo, cart, tmp_path, work=work)
    assert "card_branch" not in result
    assert result["phases"] and all("card_branch" not in p for p in result["phases"])


def test_a_resumed_run_does_not_add_the_key(repo, cart, tmp_path) -> None:  # noqa: F811
    work = initiative(two_phases=False)
    work["items"] = work["items"][:1]  # one task: the second task's patch would not re-apply on the kept branch
    drive(repo, cart, tmp_path, work=work)
    result, _ = drive(repo, cart, tmp_path, work=work, run_id="epic-2", resume_from="epic-1")
    assert "card_branch" not in result
    assert result["phases"] and all("card_branch" not in p for p in result["phases"])
