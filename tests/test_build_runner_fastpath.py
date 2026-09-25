import pytest
import yaml

import runner.anthropic_runner as anthropic_runner
from harness import runners
from harness.runners import build_runner
from runner import ScriptedRunner
from runner.system_one import FastPathRunner

ROLE = {"mode": "shadow", "threshold": 0.9}
OFF = {"handoff": {"mode": "off", "threshold": 0.5}}


class _Decider:
    def __init__(self, model, key, block=None):
        self.model, self.key = model, key


class _Real:
    def __init__(self, profile, **kwargs):
        self.profile = profile


@pytest.fixture(autouse=True)
def stubs(monkeypatch):
    monkeypatch.setattr(anthropic_runner, "AnthropicRunner", _Real)
    monkeypatch.setitem(runners._BACKENDS, "jev", _Decider)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")


def _profile(tmp_path, block=None, **extra):
    body = {"profile": "p", "tiers": {"cheap": "haiku", "standard": "sonnet", "deep": "opus"}} | extra
    if block is not None:
        body["system_one"] = block
    path = tmp_path / "p.yaml"
    path.write_text(yaml.safe_dump(body), encoding="utf-8")
    return path


def _block(**over):
    return {"backend": "jev", "model": "jev-2.1", "roles": {"handoff": ROLE}} | over


def _build(path):
    return build_runner(scripted=None, provider_profile=path)


def test_no_block_returns_the_unwrapped_runner(tmp_path) -> None:
    assert type(_build(_profile(tmp_path))) is _Real


def test_all_roles_off_returns_the_unwrapped_runner(tmp_path) -> None:
    assert type(_build(_profile(tmp_path, _block(roles=OFF)))) is _Real


def test_the_wrap_is_off_by_identity_not_by_a_pass_through(tmp_path, monkeypatch) -> None:
    real = _Real({})
    monkeypatch.setattr(anthropic_runner, "AnthropicRunner", lambda *a, **k: real)
    assert _build(_profile(tmp_path)) is real
    assert _build(_profile(tmp_path, _block(roles=OFF))) is real


def test_a_shadow_role_wraps_the_real_runner_with_the_decider(tmp_path) -> None:
    runner = _build(_profile(tmp_path, _block()))
    assert isinstance(runner, FastPathRunner)
    assert type(runner._inner) is _Real
    assert (runner._decider.model, runner._decider.key) == ("jev-2.1", "k")


def test_the_key_comes_from_the_profiles_auth_env(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MY_KEY", "other")
    runner = _build(_profile(tmp_path, _block(), auth_env="MY_KEY"))
    assert runner._decider.key == "other"


def test_a_claude_code_profile_is_wrapped_too(tmp_path) -> None:
    from runner.claude_code_runner import ClaudeCodeRunner

    runner = _build(_profile(tmp_path, _block(), runner="claude-code"))
    assert isinstance(runner, FastPathRunner)
    assert isinstance(runner._inner, ClaudeCodeRunner)


def test_the_harness_configures_the_inner_runner_through_the_wrapper(tmp_path) -> None:
    from runner.claude_code_runner import ClaudeCodeRunner

    runner = _build(_profile(tmp_path, _block(), runner="claude-code"))
    inner = runner._inner
    names = ("runs_dir", "run_id", "node_cap_usd", "repo_dir", "repo_digest", "check_commands", "verify_by_task")
    assert all(hasattr(runner, n) for n in names)
    for n, v in zip(names, (tmp_path, "r-1", 0.5, tmp_path / "wt", "map", ["pytest"], {"t": ["x"]})):
        setattr(runner, n, v)
    assert (inner.run_id, inner.node_cap_usd, inner.repo_dir) == ("r-1", 0.5, tmp_path / "wt")
    assert (inner.check_commands, inner.verify_by_task) == (["pytest"], {"t": ["x"]})
    assert "run_id" not in vars(runner)
    assert runner.calls is inner.calls
    assert runner.close.__self__ is inner
    assert isinstance(inner, ClaudeCodeRunner)


def test_scripted_is_never_wrapped_whatever_the_profile_says(tmp_path) -> None:
    responses = tmp_path / "r.json"
    responses.write_text("{}", encoding="utf-8")
    runner = build_runner(scripted=responses, provider_profile=_profile(tmp_path, _block()))
    assert type(runner) is ScriptedRunner


def test_an_unknown_backend_raises_naming_the_field(tmp_path) -> None:
    with pytest.raises(ValueError, match=r"system_one\.backend 'nope'"):
        _build(_profile(tmp_path, _block(backend="nope")))


def test_a_bad_model_id_raises_naming_the_field(tmp_path) -> None:
    with pytest.raises(ValueError, match=r"system_one: model .*latest"):
        _build(_profile(tmp_path, _block(model="jev-latest")))


def test_a_bad_model_raises_even_when_every_role_is_off(tmp_path) -> None:
    with pytest.raises(ValueError, match="model"):
        _build(_profile(tmp_path, _block(model="jev-latest", roles=OFF)))


def test_a_bad_role_setting_raises_naming_the_role(tmp_path) -> None:
    with pytest.raises(ValueError, match=r"roles\.handoff"):
        _build(_profile(tmp_path, _block(roles={"handoff": {"mode": "loud", "threshold": 0.5}})))


def _off_line(capsys) -> str:
    return capsys.readouterr().err


def test_a_missing_key_leaves_the_runner_unwrapped_and_says_why(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert type(_build(_profile(tmp_path, _block()))) is _Real
    assert _off_line(capsys) == "system-one: off ($ANTHROPIC_API_KEY (the profile's auth_env) is not set)\n"


def test_an_import_error_from_the_backend_is_unavailable(tmp_path, monkeypatch, capsys) -> None:
    def missing(model, key, block):
        raise ImportError("sdk is not installed")

    monkeypatch.setitem(runners._BACKENDS, "jev", missing)
    assert type(_build(_profile(tmp_path, _block()))) is _Real
    assert _off_line(capsys) == "system-one: off (sdk is not installed)\n"


def _knn(tmp_path, **over):
    return _profile(tmp_path, _block(backend="knn-local", model="knn-1") | over)


def test_knn_local_without_examples_is_unavailable(tmp_path, capsys) -> None:
    assert type(_build(_knn(tmp_path))) is _Real
    assert _off_line(capsys) == "system-one: off (system_one.examples names no examples file for backend knn-local)\n"


def test_knn_local_with_a_missing_examples_file_is_unavailable(tmp_path, capsys) -> None:
    missing = tmp_path / "none.jsonl"
    assert type(_build(_knn(tmp_path, examples=str(missing)))) is _Real
    assert _off_line(capsys) == f"system-one: off (examples file {missing} does not exist)\n"


def test_knn_local_expands_a_tilde_in_the_examples_path(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert type(_build(_knn(tmp_path, examples="~/none.jsonl"))) is _Real
    assert _off_line(capsys) == f"system-one: off (examples file {tmp_path}/none.jsonl does not exist)\n"


def test_knn_local_with_an_empty_examples_file_is_unavailable(tmp_path, capsys) -> None:
    empty = tmp_path / "empty.jsonl"
    empty.write_text("\n", encoding="utf-8")
    assert type(_build(_knn(tmp_path, examples=str(empty)))) is _Real
    assert _off_line(capsys) == f"system-one: off (examples file {empty} is empty)\n"


def test_the_reason_goes_to_the_notes_channel_when_one_is_given(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    lines: list[str] = []
    real = _Real({})
    profile = yaml.safe_load(_profile(tmp_path, _block()).read_text(encoding="utf-8"))
    assert runners._with_fast_path(real, profile, lines.append) is real
    assert len(lines) == 1 and lines[0].startswith("system-one: off (")


def test_a_malformed_block_still_raises_and_says_nothing(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    with pytest.raises(ValueError, match="model"):
        _build(_profile(tmp_path, _block(model="jev-latest")))
    assert _off_line(capsys) == ""


def test_an_available_backend_still_wraps_and_says_nothing(tmp_path, capsys) -> None:
    assert isinstance(_build(_profile(tmp_path, _block())), FastPathRunner)
    assert _off_line(capsys) == ""
