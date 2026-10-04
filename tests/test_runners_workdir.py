import runner.anthropic_runner
import runner.openai_compatible_runner
from harness.runners import _anthropic, _openai_compatible


class _Stub:
    def __init__(self, profile, role_skills=None):
        pass


def test_anthropic_factory_sets_cwd_and_repo_dir_after_build(monkeypatch):
    monkeypatch.setattr(runner.anthropic_runner, "AnthropicRunner", _Stub)
    built = _anthropic({}, role_skills={}, workdir="/w", repo="/r")
    assert (built.cwd, built.repo_dir) == ("/w", "/r")


def test_openai_compatible_factory_sets_cwd_and_repo_dir_after_build(monkeypatch):
    monkeypatch.setattr(runner.openai_compatible_runner, "OpenAICompatibleRunner", _Stub)
    built = _openai_compatible({}, role_skills={}, workdir="/w", repo="/r")
    assert (built.cwd, built.repo_dir) == ("/w", "/r")


def test_factories_set_nothing_when_workdir_and_repo_are_none(monkeypatch):
    monkeypatch.setattr(runner.anthropic_runner, "AnthropicRunner", _Stub)
    monkeypatch.setattr(runner.openai_compatible_runner, "OpenAICompatibleRunner", _Stub)
    built = [
        _anthropic({}, role_skills={}, workdir=None, repo=None),
        _openai_compatible({}, role_skills={}, workdir=None, repo=None),
    ]
    assert [(hasattr(b, "cwd"), hasattr(b, "repo_dir")) for b in built] == [(False, False)] * 2
