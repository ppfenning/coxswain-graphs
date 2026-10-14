"""A tilde-prefixed repo is expanded once at each entry, before git or the checks see it."""

from __future__ import annotations

import sys

import harness.cli as cli
from tests.test_epic_driver import cart, drive, initiative, repo  # noqa: F401 -- cart and repo are fixtures


def _tilde(name: str) -> str:
    return "~" + "/" + name


def test_the_parsed_repo_of_an_epic_invocation_is_the_path_under_home(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    seen = {}

    def fake_launch(specs, parser, args, *, report_out):
        seen["repo"] = args.repo
        return 0

    monkeypatch.setattr(cli, "_launch", fake_launch)
    argv = ["epic", "--initiative", "demo", "--unverified-skills", "--team", "t", "--repo"]
    assert cli._main([*argv, _tilde("repo")]) == 0
    assert seen["repo"] == str(tmp_path / "repo")
    assert cli._main([*argv, str(tmp_path / "abs")]) == 0
    assert seen["repo"] == str(tmp_path / "abs")


def test_run_epic_given_the_tilde_form_reads_agent_checks_from_the_repo_under_home(
    monkeypatch, repo, cart, tmp_path  # noqa: F811
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (repo / ".agent-checks").write_text(f'{sys.executable} -c "pass"\n', encoding="utf-8")
    result, _ = drive(_tilde(repo.name), cart, tmp_path, work=initiative(two_phases=False))
    landed = next(t for t in result["tasks"] if t["id"] == "t1-probe")
    assert f"checks:{sys.executable}" in {row["check"] for row in landed["evidence"]}
