from __future__ import annotations

import os

from graphs.delivery.initiative_decompose import initiative_text

IDEA = {"id": "i1", "title": "t", "budget_usd": 3, "why": "w"}
HOME = os.sep.join(["", "home", "someone"])


def _text(repo: str, **kwargs: str) -> str:
    return initiative_text(IDEA, ["p1"], {"p1": "g"}, repo, **kwargs)


def _repo_line(text: str) -> str:
    return next(line for line in text.splitlines() if line.startswith("repo:"))


def test_a_repo_under_the_given_home_is_written_in_the_tilde_form() -> None:
    repo = os.sep.join([HOME, "repos", "x"])
    assert _repo_line(_text(repo, home=HOME)) == "repo: " + os.sep.join(["~", "repos", "x"])


def test_a_bare_name_and_a_value_already_in_tilde_form_are_written_unchanged() -> None:
    tilde = os.sep.join(["~", "repos", "x"])
    assert _repo_line(_text("x", home=HOME)) == "repo: x"
    assert _repo_line(_text(tilde, home=HOME)) == "repo: " + tilde


def test_a_path_outside_the_home_and_a_sibling_prefix_are_written_unchanged() -> None:
    outside = os.sep.join(["", "srv", "x"])
    sibling = HOME + "2" + os.sep + "x"
    assert _repo_line(_text(outside, home=HOME)) == "repo: " + outside
    assert _repo_line(_text(sibling, home=HOME)) == "repo: " + sibling


def test_with_no_home_the_text_is_the_same_as_before() -> None:
    repo = os.sep.join([HOME, "repos", "x"])
    expected = (
        "---\n"
        "id: i1\n"
        "title: t\n"
        f"repo: {repo}\n"
        "budget_usd: 3\n"
        "---\n\n"
        "w\n\n"
        "PHASE GOALS, each judged against ITS OWN line:\n"
        "- p1: g\n"
    )
    assert _text(repo) == expected
