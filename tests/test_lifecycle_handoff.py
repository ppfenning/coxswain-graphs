"""The shuttle never treats a measured fact as missing evidence."""

from __future__ import annotations

from graphs.delivery import lifecycle_propose
from runner import ScriptedRunner


def test_measured_facts_covers_files_lines_and_pytest_commands() -> None:
    build = {
        "commands_run": [
            {"command": "pytest -q", "output": "1 failed\n1 passed\n2 passed"},
            {"command": "git status", "output": "clean"},
        ],
    }
    change_facts = {"files_touched": ["src/a.py"], "added_lines": 2, "removed_lines": 1, "changed_lines": 3}
    facts = lifecycle_propose.measured_facts(build, change_facts)
    assert facts["file:src/a.py"] == "touched: src/a.py"
    assert facts["changed_lines"] == "changed lines: 3"
    assert "pytest -q ->" in facts["pytest:0"] and "2 passed" in facts["pytest:0"]
    assert "pytest:1" not in facts


def test_prune_missing_discharges_measured_complaints_but_keeps_a_real_one() -> None:
    facts = {"changed_lines": "changed lines: 200", "pytest:0": "pytest -q -> 3 passed"}
    missing = [
        "no line count for the diff",
        "no pytest output attached",
        "the section index page is at the wrong path",
    ]
    kept, discharged = lifecycle_propose.prune_missing(missing, facts)
    assert kept == ["the section index page is at the wrong path"]
    assert len(discharged) == 2
    assert all("->" in item for item in discharged)


def test_a_handoff_with_only_measured_gaps_comes_back_complete_and_discharged() -> None:
    handoff_response = {
        "complete": False,
        "blocking": True,
        "missing": ["no test output was attached", "files touched not listed"],
        "brief": "looks fine otherwise",
    }
    scripted = ScriptedRunner({"handoff": handoff_response})
    build = {
        "patch": "--- a/src/a.py\n+++ b/src/a.py\n",
        "files_touched": ["src/a.py"],
        "commands_run": [{"command": "pytest -q", "output": "3 passed"}],
    }
    facts = lifecycle_propose._change_facts(build)
    result = lifecycle_propose._handoff(
        scripted, context=[], ticket="TICKET-1", plan={}, build=build, facts=facts
    )
    assert result["complete"] is True
    assert result["blocking"] is False
    assert result["missing"] == []
    assert len(result["discharged"]) == 2


def test_a_size_overrun_is_a_disclosed_deviation_not_a_missing_item() -> None:
    handoff_response = {"complete": True, "blocking": False, "missing": [], "brief": "ships clean"}
    scripted = ScriptedRunner({"handoff": handoff_response})
    build = {
        "patch": "diff --git a/x b/x\n--- a/x\n+++ b/x\n" + "+line\n" * 200,
        "files_touched": ["x"],
        "commands_run": [],
    }
    facts = lifecycle_propose._change_facts(build)
    result = lifecycle_propose._handoff(
        scripted, context=[], ticket="do it in ~160 lines", plan={}, build=build, facts=facts
    )
    assert "deviation: 200 source lines against ~160 (tests 0 lines, not counted)" in result["brief"]

    small_build = {"patch": "diff --git a/x b/x\n--- a/x\n+++ b/x\n+line\n", "files_touched": ["x"], "commands_run": []}
    small_facts = lifecycle_propose._change_facts(small_build)
    small_scripted = ScriptedRunner({"handoff": dict(handoff_response)})
    small_result = lifecycle_propose._handoff(
        small_scripted, context=[], ticket="do it in ~160 lines", plan={}, build=small_build, facts=small_facts
    )
    assert "deviation" not in small_result["brief"]


def test_change_facts_splits_source_lines_from_test_lines() -> None:
    patch = (
        "diff --git a/src/a.py b/src/a.py\n"
        "--- a/src/a.py\n"
        "+++ b/src/a.py\n"
        "+source line one\n"
        "+source line two\n"
        "-old source line\n"
        "diff --git a/tests/test_a.py b/tests/test_a.py\n"
        "--- a/tests/test_a.py\n"
        "+++ b/tests/test_a.py\n"
        "+test line one\n"
        "+test line two\n"
        "+test line three\n"
    )
    facts = lifecycle_propose._change_facts({"patch": patch, "files_touched": ["src/a.py", "tests/test_a.py"]})
    assert facts["source_lines"] == 3
    assert facts["test_lines"] == 3
    assert facts["changed_lines"] == 6


def test_is_test_path_matches_the_four_shapes_and_not_a_fifth() -> None:
    assert lifecycle_propose.is_test_path("tests/foo.py")
    assert lifecycle_propose.is_test_path("graphs/test_foo.py")
    assert lifecycle_propose.is_test_path("graphs/foo_test.py")
    assert lifecycle_propose.is_test_path("graphs/conftest.py")
    assert not lifecycle_propose.is_test_path("graphs/foo.py")


def test_size_deviation_binds_source_lines_only() -> None:
    deviation = lifecycle_propose._size_deviation("do it in ~50 lines", 80, 500)
    assert deviation == "deviation: 80 source lines against ~50 (tests 500 lines, not counted)"


def test_test_ratio_deviation_fires_at_4x_not_2x() -> None:
    assert lifecycle_propose._test_ratio_deviation(10, 40) == "deviation: tests are 4.0x the source"
    assert lifecycle_propose._test_ratio_deviation(10, 20) is None


def test_prune_missing_matches_phrases_as_whole_words_only():
    # "cleanliness" contains the letters of "lines"; it is not a size complaint.
    kept, discharged = lifecycle_propose.prune_missing(
        ["output of the cleanliness check was not attached", "the size against the ~90 line target"],
        {"lines": "added 40, removed 3, changed 43"},
    )
    assert kept == ["output of the cleanliness check was not attached"]
    assert discharged == ["the size against the ~90 line target -> added 40, removed 3, changed 43"]


def test_a_headerless_patch_counts_as_source():
    facts = lifecycle_propose._change_facts({"patch": "+a = 1\n+b = 2\n-c = 3\n", "files_touched": []})
    assert (facts["source_lines"], facts["test_lines"]) == (3, 0)
