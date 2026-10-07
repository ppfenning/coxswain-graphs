import ast
from pathlib import Path

from harness import cause_rule
from harness.cause_rule import CAUSES, OUTSIDE_SURFACES_WHY, classify_cause, is_auth_failure


def test_the_closed_set_of_causes():
    assert CAUSES == ("ticket", "code", "review", "harness", "unknown")


def test_a_patch_apply_failure_is_harness():
    assert classify_cause("unverified", "patch did not apply: error: corrupt patch") == "harness"


def test_a_worktree_failure_is_harness():
    assert classify_cause("refused", "worktree b could not be created: exists") == "harness"


def test_kind_infra_is_harness():
    assert classify_cause("infra", "the apply arm raised") == "harness"


def test_a_no_work_that_hit_the_budget_limit_is_harness():
    reason = "node 'build' failed in claude: error_max_budget_usd"
    assert classify_cause("no_work", reason) == "harness"


def test_a_no_work_that_failed_to_authenticate_is_harness():
    reason = (
        "node 'build' failed in claude: Failed to authenticate. API Error: 401 "
        '{"type":"error","error":{"type":"authentication_error","message":"OAuth session expired"}}'
    )
    assert classify_cause("no_work", reason) == "harness"


def test_is_auth_failure_is_true_for_an_expired_session_failure():
    reason = (
        "graphs-todo-is-not-ready-core: node 'scope_epic' failed in claude: "
        '{"subtype": "success", "result": "Failed to authenticate: OAuth session expired and could not be '
        'refreshed", "num_turns": 1, "duration_ms": 40}'
    )
    assert is_auth_failure(reason) is True


def test_is_auth_failure_is_false_for_a_failed_check():
    assert is_auth_failure("x: node 'build' failed in claude: configured check failed") is False


def test_a_no_work_with_no_authentication_marker_is_still_ticket():
    assert classify_cause("no_work", "scope found nothing to build") == "ticket"


def test_a_configured_check_failure_is_code():
    assert classify_cause("unverified", "a configured check failed: pytest") == "code"


def test_the_older_plural_configured_checks_failure_is_code():
    assert classify_cause("unknown", "configured checks failed: tests — see evidence") == "code"


def test_an_outside_surfaces_check_failure_is_ticket():
    reason = "configured check failed outside surfaces: tests/test_x.py"
    assert classify_cause("unverified", reason) == "ticket"
    assert OUTSIDE_SURFACES_WHY == "tests outside surfaces"


def test_a_plain_configured_check_failure_is_still_code():
    assert classify_cause("unverified", "configured check failed: pytest") == "code"


def test_the_outside_surfaces_phrase_mid_string_is_not_ticket():
    reason = "x: configured check failed outside surfaces: tests/test_x.py"
    assert classify_cause("unverified", reason) == "code"


def test_kind_no_work_is_ticket():
    assert classify_cause("no_work", "nothing to build") == "ticket"


def test_no_matching_rule_returns_none():
    assert classify_cause("refused", "the reviewer asked for changes") is None


def test_the_module_itself_imports_only_typing_and_future():
    # `import harness.cause_rule` runs harness/__init__, which loads the store,
    # so the module's own import statements are checked, not sys.modules.
    tree = ast.parse(Path(cause_rule.__file__).read_text())
    named = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    named |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert named == {"__future__", "typing"}
