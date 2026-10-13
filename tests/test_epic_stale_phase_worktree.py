"""An epic clears a dead run's phase worktree before it opens the phase, and only a dead run's."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from harness.epic import _Ctx, _open_phase_worktree

PHASE = "p1-foundations"


def _git(*args: str, cwd: Path) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout


@pytest.fixture
def repo(tmp_path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git("init", "-q", "-b", "main", cwd=root)
    (root / "README.md").write_text("# demo\n", encoding="utf-8")
    _git("add", "-A", cwd=root)
    _git("commit", "-qm", "base", cwd=root)
    return root


def _ctx(repo: Path, tmp_path: Path, run_id: str) -> _Ctx:
    return _Ctx(
        repo=repo, cartridge={}, runner=None, specs={}, run_id=run_id, date="d",
        max_parallel=1, ledger_path=tmp_path / "ledger.jsonl", provider_profile="p",
        runs_dir=tmp_path / "runs", worktree_root=tmp_path / "worktrees", assume=None,
        fix_attempts=None, initiative_id="demo-initiative", default_ref="main",
    )


def _finished_pid() -> int:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def _earlier_run_holding_the_branch(repo: Path, tmp_path: Path, pid: int) -> Path:
    """Run `r1` opens the phase and is left holding it; its pidfile names `pid`."""
    first = _ctx(repo, tmp_path, "r1")
    ok, detail, _ = _open_phase_worktree(first, PHASE, "main")
    assert ok, detail
    first.runs_dir.mkdir(parents=True, exist_ok=True)
    (first.runs_dir / "r1.pid").write_text(f"{pid}\n", encoding="utf-8")
    return first.phase_worktree(PHASE)


def test_a_dead_runs_phase_worktree_is_cleared_and_the_branch_reused(repo, tmp_path) -> None:
    stale = _earlier_run_holding_the_branch(repo, tmp_path, _finished_pid())
    assert stale.is_dir()

    opened = _open_phase_worktree(_ctx(repo, tmp_path, "r2"), PHASE, "main")

    ok, detail, reused = opened
    assert ok, detail
    assert reused is True
    assert not stale.exists()
    assert opened.cleared == (str(stale),)
    assert (tmp_path / "worktrees" / "r2" / PHASE).is_dir()


def test_a_live_runs_phase_worktree_is_left_and_the_open_fails_as_before(repo, tmp_path) -> None:
    stale = _earlier_run_holding_the_branch(repo, tmp_path, os.getpid())

    opened = _open_phase_worktree(_ctx(repo, tmp_path, "r2"), PHASE, "main")

    ok, detail, _ = opened
    assert not ok
    assert "already used by worktree" in detail
    assert stale.is_dir()
    assert opened.cleared == ()


def test_a_missing_pidfile_reads_as_not_alive(repo, tmp_path) -> None:
    stale = _earlier_run_holding_the_branch(repo, tmp_path, os.getpid())
    (tmp_path / "runs" / "r1.pid").unlink()

    opened = _open_phase_worktree(_ctx(repo, tmp_path, "r2"), PHASE, "main")

    assert opened[0], opened[1]
    assert opened.cleared == (str(stale),)


def test_nothing_is_cleared_when_no_other_run_holds_the_branch(repo, tmp_path) -> None:
    first = _ctx(repo, tmp_path, "r1")
    ok, detail, reused = _open_phase_worktree(first, PHASE, "main")
    assert (ok, reused) == (True, False), detail
    _git("worktree", "remove", "--force", str(first.phase_worktree(PHASE)), cwd=repo)

    opened = _open_phase_worktree(_ctx(repo, tmp_path, "r2"), PHASE, "main")

    assert opened[:1] == (True,)
    assert opened[2] is True
    assert opened.cleared == ()
