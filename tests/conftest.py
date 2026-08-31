"""Synthetic fixtures only. Obvious fakes, never a sampled workspace."""

from __future__ import annotations

import pytest

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
