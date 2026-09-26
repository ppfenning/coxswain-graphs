from core.workstore import body_sha

from harness.rescue_select import RESCUE_KIND, eligible, patch_of, rescue_cause

PATCH = "diff --git a/x b/x\n"
BODY = "current ticket text"


def _item(*attempts):
    return {"body": BODY, "attempts": [{"body_sha": body_sha(BODY), **a} for a in attempts]}


def test_cause_harness_with_a_patch_is_eligible():
    assert eligible(_item({"kind": "quarantine", "cause": "harness"}), PATCH) == (True, "harness cause, patch kept")


def test_cause_code_is_refused():
    assert eligible(_item({"kind": "quarantine", "cause": "code"}), PATCH)[0] is False


def test_cause_review_is_refused():
    assert eligible(_item({"kind": "quarantine", "cause": "review"}), PATCH)[0] is False


def test_cause_ticket_is_refused():
    assert eligible(_item({"kind": "quarantine", "cause": "ticket"}), PATCH)[0] is False


def test_no_patch_is_refused():
    assert eligible(_item({"kind": "quarantine", "cause": "harness"}), None) == (False, "no patch kept")


def test_a_blank_patch_is_refused():
    assert eligible(_item({"kind": "quarantine", "cause": "harness"}), "  \n")[0] is False


def test_no_attempts_is_refused():
    assert eligible(_item(), PATCH)[0] is False


def test_a_prior_rescue_failed_on_the_current_body_is_refused():
    item = _item({"kind": "quarantine", "cause": "harness"}, {"kind": RESCUE_KIND, "cause": "harness"})
    assert eligible(item, PATCH) == (False, "a rescue already failed on this ticket version")


def test_a_rescue_failed_on_an_older_body_does_not_block():
    item = {
        "body": BODY,
        "attempts": [
            {"kind": RESCUE_KIND, "cause": "code", "body_sha": body_sha("older ticket text")},
            {"kind": "quarantine", "cause": "harness", "body_sha": body_sha(BODY)},
        ],
    }
    assert eligible(item, PATCH) == (True, "harness cause, patch kept")


def test_patch_of_a_budget_stopped_result_is_its_kept_build_patch():
    assert patch_of({"build": {"patch": PATCH}, "fix_loop": {"stopped": "budget"}}) == PATCH


def test_patch_of_a_blank_build_patch_is_none():
    assert patch_of({"build": {"patch": " "}}) is None


def test_patch_of_none_is_none():
    assert patch_of(None) is None


def test_no_patch_refuses_without_reading_the_item():
    assert eligible({}, None) == (False, "no patch kept")


def test_a_failed_check_is_a_code_cause():
    assert rescue_cause(True) == ("code", "rule: rescue checks failed")


def test_a_review_revise_is_a_review_cause():
    assert rescue_cause(False) == ("review", "rule: rescue review revised")
