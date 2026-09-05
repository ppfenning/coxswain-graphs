"""Resume: an approved patch from an earlier run is reused, everything else runs again."""

from __future__ import annotations

from harness.resume import load_result, result_path, reusable, save_result

APPROVED = {"ticket": "t1", "build": {"patch": "--- a/f\n+++ b/f\n@@ -1 +1 @@\n-x\n+y\n"}, "proposals": [{"kind": "draft_pr_create"}]}


def test_an_approved_result_with_a_patch_is_reusable() -> None:
    assert reusable(APPROVED)


def test_anything_short_of_that_is_not() -> None:
    assert not reusable(None)
    assert not reusable({**APPROVED, "proposals": []}), "no proposals means the verdict was not approve"
    assert not reusable({**APPROVED, "build": {"patch": "   "}}), "nothing to apply"
    assert not reusable({**APPROVED, "fix_loop": {"stopped": "attempts_exhausted"}})


def test_a_proposal_that_is_not_draft_pr_create_is_not_approval() -> None:
    """`scope_epic` appends an `item_create` proposal regardless of verdict; that is not approval."""
    assert not reusable({**APPROVED, "proposals": [{"kind": "item_create"}]})


def test_a_budget_stopped_result_is_not_reusable_even_with_an_approved_proposal() -> None:
    """A kept patch is still not a resumable one: `fix_loop.stopped` means the loop never reached approve."""
    assert not reusable({**APPROVED, "fix_loop": {"attempts": 2, "stopped": "budget"}})


def test_results_round_trip_beside_the_manifest(tmp_path) -> None:
    path = save_result(APPROVED, runs_dir=tmp_path, run_id="r1", phase="p1", task="t1")
    assert path == result_path(tmp_path, "r1", "p1", "t1") and path.is_file()
    assert load_result(tmp_path, "r1", "p1", "t1") == APPROVED
    assert load_result(tmp_path, "r1", "p1", "absent") is None
    assert load_result(tmp_path, "nope", "p1", "t1") is None
