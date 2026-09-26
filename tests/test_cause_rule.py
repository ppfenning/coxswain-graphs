import ast
from pathlib import Path

from harness import cause_rule
from harness.cause_rule import CAUSES, classify_cause


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


def test_a_configured_check_failure_is_code():
    assert classify_cause("unverified", "a configured check failed: pytest") == "code"


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
