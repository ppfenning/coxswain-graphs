"""Synthetic fixtures only. Obvious fakes, never a sampled workspace."""

from __future__ import annotations

import pathlib

import pytest

# The graphs never import the substrate; only the harness does. That seam is what
# lets CI run without agent-cartridges (a private sibling repo needing a token
# the fork of a PR will not have) — but only if the tests that DO need it are
# skipped rather than failing at collection.
#
# Discovered by reading, not by a hand-maintained list: a list drifts the moment
# someone adds a test, which is exactly what happened when test_phase_runner.py
# arrived and CI still only knew about test_shell_autonomy.py.
try:  # pragma: no cover - depends on what is installed
    import core  # noqa: F401

    collect_ignore: list[str] = []
except ImportError:  # pragma: no cover
    _here = pathlib.Path(__file__).parent
    collect_ignore = [
        path.name
        for path in sorted(_here.glob("test_*.py"))
        if any(
            marker in path.read_text(encoding="utf-8")
            # the harness imports the substrate; the graphs never do
            for marker in ("from shell import", "import shell", "from harness", "import harness")
        )
    ]

CARTRIDGE = {
    "team": "acme",
    "cartridge_sha": "sha-fixture",
    "cartridge_dir": "/fake/cartridges/acme",
    "context": [],
    "skills": {"plan": "acme-skills:plan", "build": "acme-skills:build"},
    "write_kinds": {
        "draft_pr_create": {"risk": "low", "ramp": "eligible"},
        "comment_add": {"risk": "low", "ramp": "gated"},
        "merge": {"risk": "high", "ramp": "never"},
    },
    "landing_areas": {"worktree_root": "~/worktrees"},
}


@pytest.fixture
def cartridge() -> dict:
    return {k: (v.copy() if isinstance(v, (dict, list)) else v) for k, v in CARTRIDGE.items()}


@pytest.fixture
def plan_response() -> dict:
    return {"steps": ["read the failing test", "fix it"], "files_expected": ["src/a.py"], "out_of_scope": ["the CLI"]}


@pytest.fixture
def build_response() -> dict:
    return {
        "patch": "--- a/src/a.py\n+++ b/src/a.py\n-old line\n+new line\n+another\n",
        "summary": "fix the off-by-one",
        "files_touched": ["src/a.py"],
        "commands_run": [{"command": "pytest -q", "output": "1 passed"}],
    }


@pytest.fixture
def review_response() -> dict:
    return {"verdict": "approve", "findings": [], "rationale": "matches the charter"}
