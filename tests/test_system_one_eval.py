import json

import pytest

from harness import system_one_eval
from harness.system_one_eval import _table, main, score
from runner.system_one import Answer, RoleSetting
from runner.system_one_specs import question_for, role_specs

# (predicted, confidence, label). Costly answer "yes". Minority rows are the five labelled "no".
ROWS = [
    ("yes", 0.95, "yes"),
    ("yes", 0.9, "no"),
    ("yes", 0.6, "no"),
    ("no", 0.8, "no"),
    ("no", 0.55, "yes"),
    ("no", 0.4, "no"),
    (None, 0.0, "no"),
]


def test_score_reports_coverage_agreement_costly_and_minority_recall_per_threshold():
    low, high = score(ROWS, "yes", thresholds=(0.5, 0.9))
    assert low == {
        "threshold": 0.5,
        "covered": 5,
        "coverage": 5 / 7,
        "agreement": 2 / 5,
        "costly": 2,
        "costly_rate": 2 / 5,
        "minority_recall": 2 / 5,
        "minority_recall_covered": 1 / 5,
    }
    assert high == {
        "threshold": 0.9,
        "covered": 2,
        "coverage": 2 / 7,
        "agreement": 1 / 2,
        "costly": 1,
        "costly_rate": 1 / 2,
        "minority_recall": 2 / 5,
        "minority_recall_covered": 0.0,
    }


def test_score_of_no_rows_is_all_zero_and_never_divides_by_zero():
    (only,) = score([], "yes", thresholds=(0.5,))
    assert only["covered"] == only["coverage"] == only["agreement"] == only["costly_rate"] == 0


def test_a_table_of_no_thresholds_is_the_header_alone():
    (header,) = _table([]).splitlines()
    assert header.split()[0] == "threshold"


def test_question_for_is_the_question_each_build_returns():
    prompts = {
        "handoff": "x\nPlan: p\nSummary: s\nChange facts: f\nThe facts listed under Change facts",
        "review_charter": "x\nTask: p\nSummary: s\nPatch:\nd\n\nCite the charter",
    }
    for role, spec in role_specs().items():
        assert spec.build({"prompt": prompts[role]})[0] == question_for(role)


def test_question_for_an_unknown_role_refuses():
    with pytest.raises(ValueError, match="no system-one question"):
        question_for("nope")


class _Decider:
    """Answers `no` for the states whose `n` is in `no`, else `yes`, at 0.9. Raises for `n` in `boom`."""

    def __init__(self, boom=(), no=("1", "3")):
        self.boom, self.no, self.seen = set(boom), set(no), []

    def decide(self, question, state):
        self.seen.append(state["n"])
        if state["n"] in self.boom:
            raise RuntimeError("backend down")
        value = "no" if state["n"] in self.no else "yes"
        return Answer("noul", value, {value: 0.9}, 0.9)


# The profile's own block names a key variable, another backend, and turns handoff on.
# Only key_env may reach the eval's block.
_PROFILE = """\
tiers:
  cheap: m
system_one:
  backend: other
  model: x-2
  key_env: EVAL_KEY
  roles:
    handoff: {mode: "on", threshold: 0.9}
"""

# Indices 1, 3, 6 and 8 are the minority. Index 4 is not an answer a handoff offers.
_LABELS = ["yes", "no", "yes", "no", "maybe", "yes", "no", "yes", "no", "yes"]


@pytest.fixture
def run(tmp_path, monkeypatch, capsys):
    profile = tmp_path / "profile.yaml"
    profile.write_text(_PROFILE, encoding="utf-8")
    examples = tmp_path / "examples.jsonl"
    examples.write_text(
        "\n".join(json.dumps({"state": {"n": str(i)}, "label": label}) for i, label in enumerate(_LABELS)) + "\n",
        encoding="utf-8",
    )
    record = {"built": [], "decider_args": []}
    monkeypatch.setattr(system_one_eval, "build_runner", lambda **kw: record["built"].append(kw) or "real")

    def go(decider, *extra):
        def fake_decider(config, profile_, real):
            record["decider_args"].append((config, profile_, real))
            return decider

        monkeypatch.setattr(system_one_eval, "_decider", fake_decider)
        argv = ["--provider-profile", str(profile), "--backend", "b", "--model", "m-1.0", "--role", "handoff"]
        code = main([*argv, "--examples", str(examples), *extra])
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    go.profile, go.record = profile, record
    return go


def test_sampling_is_deterministic_for_a_seed_and_the_limit_is_honoured(run):
    first, second, other = _Decider(), _Decider(), _Decider()
    run(first, "--limit", "4", "--seed", "3")
    run(second, "--limit", "4", "--seed", "3")
    run(other, "--limit", "4", "--seed", "4")
    assert len(first.seen) == 4
    assert first.seen == second.seen
    assert first.seen != other.seen
    assert "4" not in first.seen


def test_the_runner_and_decider_get_the_profile_path_and_a_shadow_block_carrying_only_key_env(run):
    run(_Decider(), "--tier", "reason")
    assert run.record["built"] == [{"scripted": None, "provider_profile": str(run.profile)}]
    ((config, profile, real),) = run.record["decider_args"]
    assert (config.backend, config.model) == ("b", "m-1.0")
    assert config.roles == {"handoff": RoleSetting(mode="shadow", threshold=0.5)}
    assert profile["system_one"] == {
        "key_env": "EVAL_KEY",
        "backend": "b",
        "model": "m-1.0",
        "tier": "reason",
        "roles": {"handoff": {"mode": "shadow", "threshold": 0.5}},
    }
    assert profile["tiers"] == {"cheap": "m"}
    assert real == "real"


def test_main_prints_the_table_labels_example_count_and_no_errors(run):
    code, out, err = run(_Decider(), "--limit", "50")
    lines = out.splitlines()
    assert code == 0
    assert lines[1].split() == ["0.5", "9", "1.000", "0.778", "2", "0.222", "0.500"]
    assert "minority_recall: 0.500" in lines
    assert "labels: no=4 yes=5" in lines
    assert "examples: 10 (9 eligible, 9 sampled)" in lines
    assert "errors: 0 of 9 sampled rows raised and count as uncovered" in lines
    assert err == ""


def test_a_raising_decider_leaves_that_row_uncovered_and_is_counted(run):
    code, out, err = run(_Decider(boom={"0", "1", "2"}), "--limit", "50", "--json")
    payload = json.loads(out)
    assert code == 0
    assert payload["results"][0]["covered"] == 6
    assert (payload["errors"], payload["first_error"]) == (3, "RuntimeError: backend down")
    assert (payload["examples"], payload["labels"]) == (10, {"no": 4, "yes": 5})
    assert "3 decider errors; first: RuntimeError: backend down" in err


def test_every_row_failing_exits_1_and_says_so(run):
    code, out, err = run(_Decider(boom={str(i) for i in range(10)}))
    assert code == 1
    assert "errors: 9 of 9 sampled rows raised and count as uncovered" in out.splitlines()
    assert "9 decider errors" in err


def test_a_backend_that_cannot_be_built_exits_2_with_a_message(run, monkeypatch):
    def no_sdk(**kw):
        raise ImportError("No module named 'anthropic'")

    monkeypatch.setattr(system_one_eval, "build_runner", no_sdk)
    code, _, err = run(_Decider())
    assert code == 2
    assert "cannot build the backend: ImportError: No module named 'anthropic'" in err


@pytest.mark.parametrize(
    ("second_line", "message"),
    [('{"state": {"n": "1"}}', "example 2 needs"), ("{not json", "example 2 is not JSON")],
)
def test_a_bad_example_exits_2_and_names_its_number(tmp_path, capsys, second_line, message):
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"state": {"n": "0"}, "label": "yes"}\n' + second_line + "\n", encoding="utf-8")
    argv = ["--provider-profile", "p", "--backend", "b", "--model", "m-1", "--role", "handoff"]
    assert main([*argv, "--examples", str(bad)]) == 2
    assert message in capsys.readouterr().err
