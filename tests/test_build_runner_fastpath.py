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
    needs_key = True

    def __init__(self, model, key, block=None):
        self.model, self.key = model, key


class _Real:
    def __init__(self, profile, **kwargs):
        self.profile = profile


@pytest.fixture(autouse=True)
def stubs(monkeypatch):
    monkeypatch.setattr(anthropic_runner, "AnthropicRunner", _Real)
    monkeypatch.setattr(runners, "_backend", lambda name: _Decider if name == "jev" else None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "provider-key")
    monkeypatch.setenv("JEV_KEY", "k")


def _profile(tmp_path, block=None, **extra):
    body = {"profile": "p", "tiers": {"cheap": "haiku", "standard": "sonnet", "deep": "opus"}} | extra
    if block is not None:
        body["system_one"] = block
    path = tmp_path / "p.yaml"
    path.write_text(yaml.safe_dump(body), encoding="utf-8")
    return path


def _block(**over):
    return {"backend": "jev", "model": "jev-2.1", "key_env": "JEV_KEY", "roles": {"handoff": ROLE}} | over


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


def test_the_key_comes_from_system_one_key_env(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MY_KEY", "other")
    runner = _build(_profile(tmp_path, _block(key_env="MY_KEY")))
    assert runner._decider.key == "other"


def test_a_hosted_backend_never_gets_the_providers_auth_env_key(tmp_path, capsys) -> None:
    block = {k: v for k, v in _block().items() if k != "key_env"}
    assert type(_build(_profile(tmp_path, block, auth_env="ANTHROPIC_API_KEY"))) is _Real
    assert _off_line(capsys) == "system-one: off (system_one.key_env names no environment variable for backend jev)\n"


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


def test_an_unregistered_backend_leaves_the_runner_unwrapped_and_says_so(tmp_path, capsys) -> None:
    assert type(_build(_profile(tmp_path, _block(backend="nope")))) is _Real
    assert _off_line(capsys) == (
        "system-one: off (system_one.backend 'nope' is not registered; "
        "install the plugin that provides it (entry-point group coxswain.system_one))\n"
    )


def test_a_backend_without_needs_key_gets_an_empty_key_and_needs_no_key_env(tmp_path, monkeypatch) -> None:
    class Local:
        def __init__(self, model, key, block):
            self.model, self.key = model, key

    monkeypatch.setattr(runners, "_backend", lambda name: Local)
    block = {k: v for k, v in _block().items() if k != "key_env"}
    runner = _build(_profile(tmp_path, block))
    assert isinstance(runner, FastPathRunner)
    assert runner._decider.key == ""


class _EntryPoint:
    def __init__(self, name, target):
        self.name, self._target = name, target

    def load(self):
        return self._target


def test_backend_loads_the_registered_entry_point_and_returns_none_for_an_unknown_name(monkeypatch) -> None:
    monkeypatch.undo()
    seen = []

    def fake_entry_points(**kwargs):
        seen.append(kwargs)
        return [_EntryPoint("jev", _Decider)]

    monkeypatch.setattr(runners.importlib.metadata, "entry_points", fake_entry_points)
    assert runners._backend("jev") is _Decider
    assert runners._backend("nope") is None
    assert seen[0] == {"group": "coxswain.system_one"}


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
    monkeypatch.delenv("JEV_KEY")
    assert type(_build(_profile(tmp_path, _block()))) is _Real
    assert _off_line(capsys) == "system-one: off ($JEV_KEY (system_one.key_env) is not set)\n"


def test_an_import_error_from_the_backend_is_unavailable(tmp_path, monkeypatch, capsys) -> None:
    def missing(model, key, block):
        raise ImportError("sdk is not installed")

    monkeypatch.setattr(runners, "_backend", lambda name: missing)
    assert type(_build(_profile(tmp_path, _block()))) is _Real
    assert _off_line(capsys) == "system-one: off (sdk is not installed)\n"


def test_the_reason_goes_to_the_notes_channel_when_one_is_given(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("JEV_KEY")
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
