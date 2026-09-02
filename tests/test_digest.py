"""The repository digest: computed by tools, never by a model, and always bounded."""

from __future__ import annotations

import os
import subprocess as sp
from pathlib import Path

import pytest

from harness.digest import build_digest


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "r"
    root.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}
    sp.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    (root / "pkg").mkdir()
    (root / "pkg" / "mod.py").write_text("import os\n\nclass Thing:\n    pass\n\n\ndef helper(x):\n    return x\n\nasync def fetch():\n    pass\n", encoding="utf-8")
    (root / "run.sh").write_text("#!/bin/sh\nmain() {\n  echo hi\n}\n", encoding="utf-8")
    (root / "page.html").write_text("<script>function notIndexed(){}</script>\n" * 3, encoding="utf-8")
    (root / "untracked.py").write_text("def nope(): pass\n", encoding="utf-8")
    sp.run(["git", "-C", str(root), "add", "pkg", "run.sh", "page.html"], check=True, env=env)
    sp.run(["git", "-C", str(root), "commit", "-qm", "init"], check=True, env=env)
    return root


def test_it_lists_tracked_files_with_line_counts(repo) -> None:
    text = build_digest(repo)
    assert text.startswith("3 tracked files")
    assert "pkg/mod.py (11)" in text and "run.sh (4)" in text and "page.html (3)" in text
    assert "untracked.py" not in text, "git ls-files is the authority on what exists"


@pytest.mark.skipif(sp.run(["which", "rg"], capture_output=True).returncode != 0, reason="ripgrep not installed")
def test_it_indexes_symbols_with_line_numbers_but_not_markup(repo) -> None:
    text = build_digest(repo)
    line = next(l for l in text.splitlines() if l.startswith("pkg/mod.py"))
    assert "Thing:3" in line and "helper:7" in line and "fetch:10" in line
    assert "main:2" in next(l for l in text.splitlines() if l.startswith("run.sh"))
    assert "notIndexed" not in text, "html is listed, never indexed"


def test_it_is_bounded_and_the_file_map_survives(repo) -> None:
    text = build_digest(repo, max_chars=120)
    assert len(text) <= 120
    assert "pkg/mod.py" in text, "symbols go before files do"


def test_a_non_repository_yields_nothing(tmp_path) -> None:
    assert build_digest(tmp_path) == ""
