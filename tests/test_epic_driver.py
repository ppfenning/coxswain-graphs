"""The swarm driver, against real repositories, real branches and real checks.

The spec's central claim is that producing is not landing: an unattended run
ends as a stack of branches and a pile of proposals, and every merge is a
decision somebody made. That claim is only testable against git, so these tests
build actual repositories in tmp_path, apply actual patches, and read the
branches afterwards rather than the driver's account of itself.

The other property under test is the phase boundary: a phase unblocks its
dependents only when the validator says the goal is met AND the merges that
carry the work onto the phase branch actually happened.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path

import pytest
from core import ledger, workstore

from graphs._spec import GraphSpec
from graphs.delivery import lifecycle_propose, phase_validate
from graphs.ops import triage_quarantine
from harness.epic import (
    _ticket_amend_ramp,
    _trace_evidence,
    branch_action,
    phase_order,
    phase_parents,
    run_epic,
    task_outcome,
)
from harness.resume import load_result, save_result
from runner.claude_code_runner import files_touched_from_patch
from runner.protocol import BudgetStop, RunnerError

SHA = "sha-fixture"
PROFILE = "anthropic-default"

TASK_IDS = ("t1-probe", "t2-bench", "t3-cutover")
PHASE_IDS = ("p1-foundations", "p2-rollout")

APPROVE = {"verdict": "approve", "findings": [], "rationale": "matches the charter"}
CHUNK_OK = {"satisfied": True, "gaps": [], "reasoning": "the description is satisfied"}
CHUNK_BAD = {
    "satisfied": False,
    "gaps": ["the probe reads nothing"],
    "reasoning": "not done",
    "defects": [{"claim": "the probe reads nothing", "where": {"file": "t1-probe.txt"}}],
}
GOAL_MET = {
    "goal_met": True,
    "partial": False,
    "missing": [],
    "quarantine_blocks_dependents": False,
    "reasoning": "the pieces add up",
}
GOAL_UNMET = {
    "goal_met": False,
    "partial": True,
    "missing": ["the bench harness never landed"],
    "quarantine_blocks_dependents": True,
    "reasoning": "one task is quarantined and the dependent path needs it",
}

# The check every task's work has to survive: a committed script in the repo, so
# it runs in whichever worktree the harness made, against the state on disk.
CHECK_SCRIPT = """\
import pathlib
import sys

bad = sorted(p.name for p in pathlib.Path(".").glob("*.txt") if p.read_text().strip() != "ok")
print(f"{len(bad)} failed" if bad else "1 passed")
sys.exit(1 if bad else 0)
"""


def git(*args: str, cwd: Path) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.email=t@invalid", "-c", "user.name=t", *args],
        cwd=cwd, capture_output=True, text=True,
    )
    assert proc.returncode == 0, f"git {' '.join(args)} failed: {proc.stderr or proc.stdout}"
    return proc.stdout.strip()


def is_ancestor(repo: Path, older: str, newer: str) -> bool:
    return subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", older, newer],
        capture_output=True,
    ).returncode == 0


def branches(repo: Path) -> list[str]:
    return sorted(git("branch", "--format=%(refname:short)", cwd=repo).splitlines())


def new_file_patch(name: str, content: str = "ok") -> str:
    return (
        f"diff --git a/{name} b/{name}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{name}\n"
        "@@ -0,0 +1 @@\n"
        f"+{content}\n"
    )


@pytest.fixture
def repo(tmp_path) -> Path:
    """A real repository with a committed check script and one file to change."""
    root = tmp_path / "repo"
    root.mkdir()
    git("init", "-q", "-b", "main", cwd=root)
    (root / "check.py").write_text(CHECK_SCRIPT, encoding="utf-8")
    (root / "README.md").write_text("# demo\n", encoding="utf-8")
    git("add", "-A", cwd=root)
    git("commit", "-qm", "base", cwd=root)
    return root


@pytest.fixture
def cart(tmp_path) -> dict:
    """A cartridge with the kinds this driver actually proposes, and their arms.

    Built here rather than extended from the shared fixture because the driver
    needs a taxonomy the shared one does not carry: the branch kinds, the
    escalation kind, and a policy block for the ramp to be read against.
    """
    return {
        "team": "acme",
        "cartridge_sha": SHA,
        "context": [],
        "skills": {
            "plan": "acme-skills:plan",
            "build": "acme-skills:build",
            "review_charter": "acme-skills:review",
            "validate_chunk": "acme-skills:validate-chunk",
            "validate_phase": "acme-skills:validate-phase",
            "work_state_arm": "acme-skills:work-state",
        },
        "write_kinds": {
            "draft_pr_create": {"risk": "low", "ramp": "eligible", "apply_arm": "shell"},
            "merge_stack": {"risk": "high", "ramp": "eligible", "apply_arm": "shell"},
            "stack_rebase": {"risk": "high", "ramp": "eligible", "apply_arm": "shell"},
            "state_move": {"risk": "low", "ramp": "deferred", "apply_arm": "work_state_arm"},
            "self_modification": {"risk": "high", "ramp": "never", "apply_arm": "pr"},
            "consolidate": {"risk": "low", "ramp": "deferred"},
        },
        "policy": {"graduation_n": 3, "regraduation_multiplier": 2, "caps": {}},
        "landing_areas": {
            "worktree_root": str(tmp_path / "worktrees"),
            "checks": [{"name": "state", "cmd": f"{sys.executable} check.py"}],
        },
    }


def initiative(*, two_phases: bool = True, done: tuple[str, ...] = ()) -> dict:
    """A synthetic initiative: p1 with two independent tasks, p2 with a dependent."""
    items = [
        {"id": "t1-probe", "phase": "p1-foundations", "state": "ready", "needs": [], "surfaces": [],
         "title": "schema probe", "body": "read the vendor schema"},
        {"id": "t2-bench", "phase": "p1-foundations", "state": "ready", "needs": [], "surfaces": [],
         "title": "bench harness", "body": "time the join"},
    ]
    if two_phases:
        items.append(
            {"id": "t3-cutover", "phase": "p2-rollout", "state": "todo", "needs": ["t1-probe"],
             "surfaces": [], "title": "cutover", "body": "move traffic"}
        )
    for item in items:
        if item["id"] in done:
            item["state"] = "done"
    return {
        "id": "demo-initiative",
        "title": "demo",
        "body": "make the vendor join measurable end to end",
        "phases": sorted({i["phase"] for i in items}),
        "items": items,
    }


class Runner:
    """Scripted by role, and by task where a role runs once per task.

    Keyed off the prompt rather than a call counter: the fan-out is concurrent,
    so a positional script would be answering whichever task happened to get
    there first. A role with nothing scripted for the task in front of it raises
    — which is exactly how a task gets quarantined without the test faking one.
    """

    def __init__(self, patches: dict[str, str], *, chunk=None, verdicts=None, style=None, review=None) -> None:
        self.patches = patches
        self.chunk = chunk or {}
        self.verdicts = verdicts or {}
        self.style = style or {}
        self.review = review or {}
        self.calls: list[dict] = []
        self.lock = threading.Lock()

    def _subject(self, prompt: str, candidates) -> str | None:
        return next((c for c in candidates if c in prompt), None)

    def run(self, *, role, tier, schema, prompt, context=(), thread=None, budget_usd=None, task=None):
        with self.lock:
            self.calls.append({"role": role, "tier": tier, "prompt": prompt, "budget_usd": budget_usd})

        if role == "plan":
            return {"steps": ["do it"], "files_expected": ["x.txt"], "out_of_scope": []}
        if role == "build":
            task = self._subject(prompt, TASK_IDS)
            if task not in self.patches:
                raise RunnerError(f"no build scripted for {task}")
            return {
                "patch": self.patches[task],
                "summary": f"built {task}",
                "files_touched": files_touched_from_patch(self.patches[task]),
                "commands_run": [],
            }
        if role == "review_charter":
            return dict(self.review.get(self._subject(prompt, TASK_IDS), APPROVE))
        if role == "validate_chunk":
            return dict(self.chunk.get(self._subject(prompt, TASK_IDS), CHUNK_OK))
        if role == "validate_phase":
            return dict(self.verdicts.get(self._subject(prompt, PHASE_IDS), GOAL_MET))
        if role == "work_state_arm":
            return {"applied": True, "detail": "state moved"}
        if role == "style_pass":
            return {"patch": self.style.get(self._subject(prompt, PHASE_IDS), "")}
        raise RunnerError(f"no scripted response for role '{role}'")


class CommandsRunner(Runner):
    """Like `Runner`, but appends a `ClaudeCodeRunner`-shaped ledger row per build call.

    `harness/epic._trace_evidence` reads `runner.calls` for a `role: "build"` row whose
    `task_id` matches the task — never `files_touched`, which two tasks can share. This
    double stamps `task_id` from the `task=` kwarg the graph now passes, same as
    `ClaudeCodeRunner.run` does, so the harness's own selection is what is under test.
    """

    def __init__(self, patches: dict[str, str], *, commands_run: dict[str, list[dict]] | None = None) -> None:
        super().__init__(patches)
        self.commands_run = commands_run or {}

    def run(self, *, role, tier, schema, prompt, context=(), thread=None, budget_usd=None, task=None):
        result = super().run(
            role=role, tier=tier, schema=schema, prompt=prompt, context=context, thread=thread, budget_usd=budget_usd
        )
        if role == "build":
            with self.lock:
                self.calls.append({
                    "role": "build",
                    "task_id": task,
                    "files_touched": [f"{task}.txt"],
                    "commands_run": self.commands_run.get(task, []),
                })
        return result


class BudgetStopArm(Runner):
    """Like `Runner`, but the `work_state_arm` bookkeeping call for one named task stops on budget."""

    def __init__(self, patches: dict[str, str], *, stops: str) -> None:
        super().__init__(patches)
        self.stops = stops

    def run(self, *, role, tier, schema, prompt, context=(), thread=None, budget_usd=None, task=None):
        if role == "work_state_arm" and self.stops in prompt:
            raise BudgetStop(role="work_state_arm", thread=None, session=None, spent_usd=0.0, detail="budget")
        return super().run(
            role=role, tier=tier, schema=schema, prompt=prompt, context=context, thread=thread, budget_usd=budget_usd
        )


SPECS = {
    "lifecycle": GraphSpec(name="lifecycle", graph_name="lifecycle-propose", run=lifecycle_propose.run),
    "validate": phase_validate.SPEC,
}


def drive(
    repo, cart, tmp_path, *, runner=None, work=None, assume="a", run_id="epic-1", patches=None, fix_attempts=None,
    keep_worktrees=False, specs=None,
):
    runner = runner or Runner(patches if patches is not None else {t: new_file_patch(f"{t}.txt") for t in TASK_IDS})
    result = run_epic(
        initiative=work if work is not None else initiative(),
        repo=repo,
        cartridge=cart,
        runner=runner,
        specs=specs if specs is not None else SPECS,
        run_id=run_id,
        date="2026-09-01",
        max_parallel=3,
        ledger_path=tmp_path / "ledger.jsonl",
        provider_profile=PROFILE,
        runs_dir=tmp_path / "runs",
        worktree_root=cart["landing_areas"]["worktree_root"],
        assume=assume,
        fix_attempts=fix_attempts,
        keep_worktrees=keep_worktrees,
    )
    return result, runner


# ── the phase graph, before anything runs ───────────────────────────────────


def test_phase_edges_are_derived_from_the_task_edges() -> None:
    parents = phase_parents(initiative()["items"])
    assert parents == {"p1-foundations": set(), "p2-rollout": {"p1-foundations"}}
    assert phase_order(parents) == (["p1-foundations", "p2-rollout"], [])


def test_a_phase_with_two_parents_is_blocked_rather_than_guessed_at(repo, cart, tmp_path) -> None:
    """One stack has one base ref; picking a parent would build on half the ground."""
    work = initiative()
    work["items"].append(
        {"id": "t0-seed", "phase": "p0-seed", "state": "ready", "needs": [], "surfaces": [],
         "title": "seed", "body": "seed"}
    )
    work["items"][-2]["needs"] = ["t1-probe", "t0-seed"]  # t3 now has two parent phases
    result, _ = drive(repo, cart, tmp_path, work=work)
    blocked = next(p for p in result["phases"] if p["phase"] == "p2-rollout")
    assert blocked["status"] == "blocked"
    assert "multiple parent phases" in blocked["reason"]


# ── the happy path ──────────────────────────────────────────────────────────


def test_the_happy_path_stacks_the_second_phase_on_the_first(repo, cart, tmp_path) -> None:
    result, _ = drive(repo, cart, tmp_path)

    p1, p2 = result["phases"]
    assert (p1["status"], p2["status"]) == ("complete", "complete")
    assert result["totals"]["phases_complete"] == 2
    assert result["totals"]["tasks_quarantined"] == 0

    # Both drafts landed and both merged into the phase branch.
    assert {b for b in branches(repo) if b.startswith("epic/demo-initiative/p1-foundations--")} == {
        "epic/demo-initiative/p1-foundations--t1-probe",
        "epic/demo-initiative/p1-foundations--t2-bench",
    }
    for task in ("t1-probe", "t2-bench"):
        assert is_ancestor(repo, f"epic/demo-initiative/p1-foundations--{task}", "epic/demo-initiative/p1-foundations")

    # The stack: p2 is branched from p1's head, which is the whole topology claim.
    assert is_ancestor(repo, "epic/demo-initiative/p1-foundations", "epic/demo-initiative/p2-rollout")
    assert is_ancestor(repo, "epic/demo-initiative/p2-rollout--t3-cutover", "epic/demo-initiative/p2-rollout")

    # Nothing reached the default branch, at any point, on any streak.
    assert git("rev-parse", "main", cwd=repo) == git("rev-parse", "main~0", cwd=repo)
    assert git("rev-list", "--count", "main", cwd=repo) == "1", "main has not moved"
    assert not is_ancestor(repo, "epic/demo-initiative/p1-foundations", "main")


# ── cleanup on exit, work-shape.md §6/§8 ────────────────────────────────────


class RaisingCloseRunner(Runner):
    """A build that succeeds, then a close that does not — the exception a
    `finally` has to survive, raised only after a worktree already exists."""

    def close(self) -> None:
        raise RuntimeError("close blew up")


def test_a_normal_run_removes_its_worktree_directory_and_registration(repo, cart, tmp_path) -> None:
    result, _ = drive(repo, cart, tmp_path, run_id="epic-clean")

    assert result["totals"]["phases_complete"] == 2
    assert not (Path(cart["landing_areas"]["worktree_root"]) / "epic-clean").exists()
    assert "epic-clean" not in git("worktree", "list", cwd=repo)


def test_a_run_that_raises_still_cleans_up_because_finally_fires(repo, cart, tmp_path) -> None:
    runner = RaisingCloseRunner({t: new_file_patch(f"{t}.txt") for t in TASK_IDS})

    with pytest.raises(RuntimeError, match="close blew up"):
        drive(repo, cart, tmp_path, runner=runner, run_id="epic-raise")

    assert not (Path(cart["landing_areas"]["worktree_root"]) / "epic-raise").exists()
    assert "epic-raise" not in git("worktree", "list", cwd=repo)


def test_keep_worktrees_moves_the_run_dir_under_kept_run_id_instead_of_deleting_it(repo, cart, tmp_path) -> None:
    drive(repo, cart, tmp_path, run_id="epic-keep", keep_worktrees=True)

    root = Path(cart["landing_areas"]["worktree_root"])
    assert not (root / "epic-keep").exists(), "the emptied run directory should not survive the move"
    kept = root / "_kept" / "epic-keep"
    for phase in ("p1-foundations", "p2-rollout"):
        phase_dir = kept / phase
        assert phase_dir.is_dir(), f"{phase_dir} missing: §6 kept shape is _kept/<run_id>/<phase>"
        assert any(phase_dir.rglob("*")), "the moved directory carried its contents, not an empty shell"
    assert not (kept / "epic-keep").exists(), "the run id must not be doubled into the kept path"
    assert "epic-keep" not in git("worktree", "list", cwd=repo)


def test_every_phase_records_its_own_manifest_and_ledger_rows(repo, cart, tmp_path) -> None:
    drive(repo, cart, tmp_path)
    written = sorted(p.name for p in (tmp_path / "runs").glob("*.json"))
    assert written == ["epic-1:p1-foundations.json", "epic-1:p2-rollout.json"]

    rows = ledger.read(tmp_path / "ledger.jsonl")
    assert {row["principal"] for row in rows} == {"epic-swarm(lifecycle-propose)"}
    assert {row["cartridge_sha"] for row in rows} == {SHA}
    merges = [row for row in rows if row["kind"] == "merge_stack"]
    assert len(merges) == 3 and {row["outcome"] for row in merges} == {"clean"}
    drafts = [row for row in rows if row["kind"] == "draft_pr_create"]
    assert len(drafts) == 3 and {row["outcome"] for row in drafts} == {"clean"}


# ── day one: the terminal state is branches and proposals ───────────────────


def test_gated_day_one_produces_branches_and_proposals_and_lands_nothing(repo, cart, tmp_path) -> None:
    """The spec's central claim, as an assertion about refs."""
    result, _ = drive(repo, cart, tmp_path, assume="r")

    drafts = [b for b in branches(repo) if "--" in b]
    assert drafts == [], "a refused gate must leave no draft branch behind"
    scratch = [b for b in branches(repo) if b.startswith("agents/epic-1/")]
    assert sorted(scratch) == ["agents/epic-1/t1-probe", "agents/epic-1/t2-bench"]

    assert result["totals"]["phases_complete"] == 0
    p1 = result["phases"][0]
    assert p1["status"] == "partial" and "nothing was merged" in p1["reason"]
    # p2 depends on p1, so it is reported blocked rather than run.
    assert result["phases"][1]["status"] == "blocked"
    assert result["proposals"], "the run still produced work to decide on"
    # The phase branch exists and is exactly where it started: the work is on
    # the scratch branches, and the gate is what would have moved it.
    assert git("rev-parse", "epic/demo-initiative/p1-foundations", cwd=repo) == git("rev-parse", "main", cwd=repo)


def test_a_refused_draft_takes_its_own_merge_with_it(repo, cart, tmp_path) -> None:
    result, _ = drive(repo, cart, tmp_path, assume="r")
    rows = ledger.read(tmp_path / "ledger.jsonl")
    assert {row["outcome"] for row in rows} == {"reversal"}
    assert all(not task["merged"] and task["draft"] is None for task in result["tasks"])


# ── quarantine, at task grain and its effect at phase grain ─────────────────


def test_a_failing_check_quarantines_that_task_and_the_sibling_still_merges(repo, cart, tmp_path) -> None:
    runner = Runner(
        {
            "t1-probe": new_file_patch("t1-probe.txt", "broken"),  # the check reads this
            "t2-bench": new_file_patch("t2-bench.txt"),
            "t3-cutover": new_file_patch("t3-cutover.txt"),
        },
        verdicts={"p1-foundations": GOAL_UNMET},
    )
    result, _ = drive(repo, cart, tmp_path, runner=runner)

    quarantined = result["quarantined"]
    assert [q["id"] for q in quarantined] == ["t1-probe"]
    assert "check failed" in quarantined[0]["reason"]
    assert quarantined[0]["kind"] == "no_work"
    assert "patch_kept" not in quarantined[0]
    assert result["totals"]["tasks_quarantined"] == 1

    # The sibling's work is untouched by its neighbour's failure.
    assert is_ancestor(repo, "epic/demo-initiative/p1-foundations--t2-bench", "epic/demo-initiative/p1-foundations")
    assert "epic/demo-initiative/p1-foundations--t1-probe" not in branches(repo)

    # The validator was told about the quarantine, and its verdict is what
    # decides the phase — which then does not unblock its dependent.
    prompt = next(c["prompt"] for c in runner.calls if c["role"] == "validate_phase")
    assert "t1-probe" in prompt and "check failed" in prompt
    assert result["phases"][0]["status"] == "partial"
    assert result["phases"][1]["status"] == "blocked"
    assert "did not meet its goal" in result["phases"][1]["reason"]
    assert result["totals"]["phases_complete"] == 0


def test_the_failing_checks_evidence_reaches_the_record(repo, cart, tmp_path) -> None:
    runner = Runner(
        {"t1-probe": new_file_patch("t1-probe.txt", "broken"), "t2-bench": new_file_patch("t2-bench.txt")},
        verdicts={"p1-foundations": GOAL_UNMET},
    )
    result, _ = drive(repo, cart, tmp_path, runner=runner, work=initiative(two_phases=False))
    failed = next(t for t in result["tasks"] if t["id"] == "t1-probe")
    assert any(row["check"] == "checks:state" and "FAIL" in row["output"] for row in failed["evidence"])


def test_trace_commands_land_in_order_and_a_self_reported_one_is_never_folded_in(repo, cart, tmp_path) -> None:
    # Deliberately not alphabetical ("ruff" then "echo") — a regression that
    # sorted commands instead of preserving trace order would fail this.
    runner = CommandsRunner(
        {"t1-probe": new_file_patch("t1-probe.txt"), "t2-bench": new_file_patch("t2-bench.txt")},
        commands_run={
            "t1-probe": [
                {"command": "ruff check .", "output": "All checks passed!", "source": "trace"},
                {"command": "echo done", "output": "done", "source": "trace"},
                {"command": "pytest -q --cov", "output": "not run", "source": "self_report"},
            ]
        },
    )
    result, _ = drive(repo, cart, tmp_path, runner=runner, work=initiative(two_phases=False))
    landed = next(t for t in result["tasks"] if t["id"] == "t1-probe")
    rows = [row for row in landed["evidence"] if row["check"] == "command"]
    assert rows == [
        {"check": "command", "source": "trace", "output": "ruff check .\nAll checks passed!"},
        {"check": "command", "source": "trace", "output": "echo done\ndone"},
    ]


def test_files_touched_appears_exactly_once(repo, cart, tmp_path) -> None:
    runner = CommandsRunner({"t1-probe": new_file_patch("t1-probe.txt")})
    result, _ = drive(repo, cart, tmp_path, runner=runner, work=initiative(two_phases=False))
    landed = next(t for t in result["tasks"] if t["id"] == "t1-probe")
    rows = [row for row in landed["evidence"] if row["check"] == "files_touched"]
    assert rows == [{"check": "files_touched", "source": "trace", "output": "t1-probe.txt"}]


def test_two_tasks_with_identical_files_touched_each_get_their_own_evidence(repo, cart, tmp_path) -> None:
    """`task_id` selects the call, not `files_touched` — which two tasks can share."""
    runner = CommandsRunner(
        {"t1-probe": new_file_patch("shared.txt"), "t2-bench": new_file_patch("shared.txt")},
        commands_run={
            "t1-probe": [{"command": "one", "output": "1", "source": "trace"}],
            "t2-bench": [{"command": "two", "output": "2", "source": "trace"}],
        },
    )
    # Both tasks' patches touch the same file, so a match keyed on
    # `files_touched` would collide; `task_id` is what disambiguates the two
    # ledger rows below.
    result, _ = drive(repo, cart, tmp_path, runner=runner, work=initiative(two_phases=False))
    t1 = next(t for t in result["tasks"] if t["id"] == "t1-probe")
    t2 = next(t for t in result["tasks"] if t["id"] == "t2-bench")
    assert [r["output"] for r in t1["evidence"] if r["check"] == "command"] == ["one\n1"]
    assert [r["output"] for r in t2["evidence"] if r["check"] == "command"] == ["two\n2"]


class RetriedCommandsRunner(CommandsRunner):
    """Reviews t1-probe's first patch as `revise` once, so the fix loop retries
    the build — two `role: "build"` ledger rows share one `task_id`. Evidence
    must come from the last, the retry whose patch the record actually applied.
    """

    def __init__(self, patches: dict[str, str]) -> None:
        super().__init__(patches, commands_run={"t1-probe": [{"command": "first", "output": "1", "source": "trace"}]})
        self._revised = False

    def run(self, *, role, tier, schema, prompt, context=(), thread=None, budget_usd=None, task=None):
        if role == "review_charter" and "t1-probe" in prompt and not self._revised:
            self._revised = True
            with self.lock:
                self.calls.append({"role": role, "tier": tier, "prompt": prompt})
            return dict(REVISE)
        if role == "build" and task == "t1-probe" and self._revised:
            self.commands_run["t1-probe"] = [{"command": "second", "output": "2", "source": "trace"}]
            # A retry that repeats the same patch reads as "no progress" and is
            # refused before a second build call is even ledgered — so the
            # retry has to change the patch to prove anything about which call
            # the evidence comes from.
            self.patches["t1-probe"] = new_file_patch("t1-probe.txt") + new_file_patch("t1-probe-retry.txt")
        return super().run(
            role=role, tier=tier, schema=schema, prompt=prompt, context=context, thread=thread,
            budget_usd=budget_usd, task=task,
        )


def test_a_retried_builds_evidence_comes_from_the_last_call(repo, cart, tmp_path) -> None:
    runner = RetriedCommandsRunner({t: new_file_patch(f"{t}.txt") for t in TASK_IDS})
    result, _ = drive(repo, cart, tmp_path, runner=runner, work=initiative(two_phases=False))
    landed = next(t for t in result["tasks"] if t["id"] == "t1-probe")
    rows = [row for row in landed["evidence"] if row["check"] == "command"]
    assert rows == [{"check": "command", "source": "trace", "output": "second\n2"}]


def test_trace_evidence_ignores_the_epics_own_style_pass_call_on_the_shared_ledger() -> None:
    """`_trim_phase` is `harness/epic.py`'s own direct `ctx.runner.run` call — `role:
    "style_pass"`, one per PHASE, carrying no `task_id` because a phase is not a task.
    Its row lands on the same shared `ctx.runner.calls` a build call's does; the
    `role == "build"` filter, not a `task_id` match, is what keeps it out of a task's
    evidence.
    """
    calls = [
        {"role": "style_pass", "task_id": None, "commands_run": [{"command": "x", "output": "y", "source": "trace"}], "files_touched": ["z.txt"]},
        {"role": "build", "task_id": "t1-probe", "commands_run": [{"command": "real", "output": "1", "source": "trace"}], "files_touched": ["t1-probe.txt"]},
    ]
    assert _trace_evidence(calls, "t1-probe", "") == [
        {"check": "command", "source": "trace", "output": "real\n1"},
        {"check": "files_touched", "source": "trace", "output": "t1-probe.txt"},
    ]


def test_a_resumed_tasks_files_touched_falls_back_to_the_patch_when_no_call_matches() -> None:
    """A resumed task (`--resume-from`) makes no build call in this run, so `calls`
    holds no `task_id`-matching row — `files_touched` must still name the reused
    patch's own files, not go empty.
    """
    assert _trace_evidence([], "t1-probe", new_file_patch("t1-probe.txt")) == [
        {"check": "files_touched", "source": "trace", "output": "t1-probe.txt"},
    ]


def test_change_facts_carries_the_full_checks_result_for_a_quarantined_task(repo, cart, tmp_path) -> None:
    """The task record a failing check actually reaches — it never becomes a survivor."""
    runner = Runner(
        {"t1-probe": new_file_patch("t1-probe.txt", "broken"), "t2-bench": new_file_patch("t2-bench.txt")},
        verdicts={"p1-foundations": GOAL_UNMET},
    )
    result, _ = drive(repo, cart, tmp_path, runner=runner, work=initiative(two_phases=False))
    failed = next(t for t in result["tasks"] if t["id"] == "t1-probe")
    assert failed["status"] == "quarantined"
    checks = failed["change_facts"]["checks"]
    assert checks is not None
    assert any(c["name"] == "state" and c["passed"] is False for c in checks)


def test_change_facts_carries_the_full_checks_result_not_just_the_evidence_summary(repo, cart, tmp_path) -> None:
    """The validator sees the whole per-check result object, not only the terse evidence line."""
    runner = Runner({"t1-probe": new_file_patch("t1-probe.txt"), "t2-bench": new_file_patch("t2-bench.txt")})
    _, runner = drive(repo, cart, tmp_path, runner=runner, work=initiative(two_phases=False))
    prompt = next(c["prompt"] for c in runner.calls if c["role"] == "validate_phase")
    assert "'id': 't1-probe'" in prompt
    assert "'name': 'state'" in prompt and "'passed': True" in prompt and "'exit_code': 0" in prompt


def test_a_failing_checks_detail_reaches_the_stored_attempt_and_the_next_ticket_body(repo, cart, tmp_path) -> None:
    """The stored attempt carries the check's name, command and tail — the terse quarantine reason does not."""
    wi = tmp_path / "wi"
    (wi / "p1-foundations").mkdir(parents=True)
    (wi / "initiative.md").write_text(
        "---\nid: demo-initiative\ntitle: demo\n---\n\nmake the vendor join measurable end to end\n"
    )
    (wi / "p1-foundations" / "t1-probe.md").write_text(
        "---\nid: t1-probe\nphase: p1-foundations\nstate: ready\nneeds: []\nsurfaces: []\n"
        "title: schema probe\n---\n\nread the vendor schema\n"
    )
    work = workstore.read_initiative(wi)

    runner = Runner({"t1-probe": new_file_patch("t1-probe.txt", "broken")})
    result, _ = drive(repo, cart, tmp_path, runner=runner, work=work, run_id="epic-checkfail")

    terse = result["quarantined"][0]["reason"]
    assert terse == "configured check failed: state — 1 failed"

    item = workstore.read_item(wi / "p1-foundations" / "t1-probe.md")
    stored = item["attempts"][0]["reason"]
    assert "state" in stored and "check.py" in stored and "exit 1" in stored and "1 failed" in stored
    assert stored != terse

    work2 = workstore.read_initiative(wi)
    runner2 = Runner({"t1-probe": new_file_patch("t1-probe.txt")})
    drive(repo, cart, tmp_path, runner=runner2, work=work2, run_id="epic-checkfail-2")
    prompt = next(c["prompt"] for c in runner2.calls if c["role"] == "build" and "t1-probe" in c["prompt"])
    assert "check.py" in prompt and "1 failed" in prompt


# ── outcome and the exit line: an approved build that does not land ─────────


def test_task_outcome_names_all_four_cases() -> None:
    assert task_outcome("approve", None, None, True) == "landed"
    assert task_outcome("approve", None, "configured checks failed: state — see evidence", False) == "approved_not_landed"
    assert task_outcome(None, "approve", "harness fault: check 'state' could not run: boom", False) == "harness_fault"
    assert task_outcome("revise", None, None, False) == "rejected"


def test_an_approved_and_quarantined_task_names_itself_in_the_exit_summary(repo, cart, tmp_path) -> None:
    runner = Runner(
        {"t1-probe": new_file_patch("t1-probe.txt", "broken"), "t2-bench": new_file_patch("t2-bench.txt")},
        verdicts={"p1-foundations": GOAL_UNMET},
    )
    result, _ = drive(repo, cart, tmp_path, runner=runner, work=initiative(two_phases=False))

    failed = next(t for t in result["tasks"] if t["id"] == "t1-probe")
    assert failed["outcome"] == "approved_not_landed"
    assert "check failed" in failed["reason"]
    assert result["totals"]["approved_not_landed"] == 1
    assert f"approved but not landed: t1-probe — cox runs recover epic-1 t1-probe --repo {repo}" in result["exit_summary"]


def test_an_apply_arms_budgetstop_quarantines_the_task_as_infra_and_the_run_ends_clean(repo, cart, tmp_path) -> None:
    """A `RunnerError` from the bookkeeping arm is quarantined, not a traceback."""
    runner = BudgetStopArm({t: new_file_patch(f"{t}.txt") for t in TASK_IDS}, stops="t1-probe")
    result, _ = drive(repo, cart, tmp_path, runner=runner, work=initiative(two_phases=False))

    entry = next(q for q in result["quarantined"] if q["id"] == "t1-probe")
    assert entry["kind"] == "infra"
    assert entry["patch_kept"] is True
    assert "work_state_arm" in entry["reason"] and "BudgetStop" in entry["reason"]

    # The task's own record agrees: merged but quarantined earns `approved_not_landed`, never `landed`.
    task_record = next(t for t in result["tasks"] if t["id"] == "t1-probe")
    assert task_record["status"] == "quarantined"
    assert task_record["quarantine"] == entry["reason"]
    assert task_record["merged"] is True
    assert task_record["outcome"] == "approved_not_landed"
    assert result["totals"]["approved_not_landed"] == 1

    # The sibling task still landed, and the run wrote a phase record and exited normally.
    assert is_ancestor(repo, "epic/demo-initiative/p1-foundations--t2-bench", "epic/demo-initiative/p1-foundations")
    assert (tmp_path / "runs" / "epic-1:p1-foundations.json").exists()


def test_a_successful_arm_call_is_unchanged(repo, cart, tmp_path) -> None:
    """The new try/except around `auto_apply` in `_execute` leaves a clean run untouched."""
    result, _ = drive(repo, cart, tmp_path, work=initiative(two_phases=False))

    assert result["quarantined"] == []
    for task in ("t1-probe", "t2-bench"):
        assert is_ancestor(repo, f"epic/demo-initiative/p1-foundations--{task}", "epic/demo-initiative/p1-foundations")
        task_record = next(t for t in result["tasks"] if t["id"] == task)
        assert task_record["merged"] is True
        assert task_record["outcome"] == "landed"


def test_two_infra_attempts_do_not_trip_the_attempt_cap(repo, cart, tmp_path) -> None:
    """`ATTEMPT_CAP` is 2: two real failures refuse a third run. Two `infra` ones must not."""
    work = initiative(two_phases=False)
    task = next(item for item in work["items"] if item["id"] == "t1-probe")
    task["attempts"] = [
        {
            "run": f"epic-prior-{n}",
            "phase": "p1-foundations",
            "reason": "apply arm 'work_state_arm' raised BudgetStop: budget",
            "kind": "infra",
            "patch_kept": True,
            "ts": "2026-09-01T00:00:00+00:00",
        }
        for n in (1, 2)
    ]
    result, _ = drive(repo, cart, tmp_path, work=work)

    assert not any(q["id"] == "t1-probe" and "attempt cap" in q["reason"] for q in result["quarantined"])
    assert is_ancestor(repo, "epic/demo-initiative/p1-foundations--t1-probe", "epic/demo-initiative/p1-foundations")


# ── ticket_amend's additive check, and the attempt cap launching triage ─────


def test_additive_ticket_amend_diff_stays_eligible() -> None:
    old = "---\nid: t1\n---\nOriginal text."
    new = old + "\n\n## 2026-09-16\nAppended note."
    assert _ticket_amend_ramp(old, new) == "eligible"


def test_a_removed_line_forces_the_amendment_gated() -> None:
    old = "---\nid: t1\n---\nLine one.\nLine two."
    new = "---\nid: t1\n---\nLine one."
    assert _ticket_amend_ramp(old, new) == "gated"


def test_a_frontmatter_change_forces_the_amendment_gated() -> None:
    old = "---\nid: t1\nstate: ready\n---\nBody text."
    new = "---\nid: t1\nstate: done\n---\nBody text.\n\nAppended."
    assert _ticket_amend_ramp(old, new) == "gated"


class TriageAttemptRunner(Runner):
    """Like `Runner`, but scripts `role="triage"` with a `ticket_defect` classification."""

    def run(self, *, role, tier, schema, prompt, context=(), thread=None, budget_usd=None, task=None):
        if role == "triage":
            with self.lock:
                self.calls.append({"role": role, "tier": tier, "prompt": prompt, "budget_usd": budget_usd})
            return {
                "class": "ticket_defect",
                "diagnosis": "the earlier attempts hit check failed: bad output",
                "cites": ["attempt-1|reason"],
                "action": "add a note describing the missing fixture",
            }
        return super().run(
            role=role, tier=tier, schema=schema, prompt=prompt, context=context, thread=thread,
            budget_usd=budget_usd, task=task,
        )


def test_the_attempt_cap_launches_triage_instead_of_a_plain_quarantine(repo, cart, tmp_path) -> None:
    work = initiative(two_phases=False)
    task = next(item for item in work["items"] if item["id"] == "t1-probe")
    task["attempts"] = [
        {"run": f"epic-prior-{n}", "phase": "p1-foundations", "reason": "check failed: bad output",
         "kind": "refused", "ts": f"2026-09-0{n}T00:00:00+00:00"}
        for n in (1, 2)
    ]
    local_cart = {**cart, "write_kinds": {**cart["write_kinds"], "ticket_amend": {
        "risk": "low", "ramp": "eligible", "apply_arm": "work_state_arm",
    }}}
    runner = TriageAttemptRunner({"t2-bench": new_file_patch("t2-bench.txt")})
    result, _ = drive(
        repo, local_cart, tmp_path, runner=runner, work=work,
        specs={**SPECS, "triage-quarantine": triage_quarantine.SPEC},
    )

    assert any(call["role"] == "triage" for call in runner.calls)
    assert not any(q["id"] == "t1-probe" for q in result["quarantined"])
    amend = next(p for p in result["proposals"] if p["target"] == "t1-probe")
    assert amend["kind"] == "ticket_amend"
    assert amend["ramp"] == "eligible"


def test_the_cli_exit_line_names_an_approved_and_unlanded_task(monkeypatch, tmp_path, capsys) -> None:
    from harness import cli

    monkeypatch.setattr(cli, "resolve_cartridge", lambda *a, **k: ({"skills": {}}, {}))
    monkeypatch.setattr(cli, "build_runner", lambda **k: object())
    monkeypatch.setattr(workstore, "read_initiative", lambda path: {"id": "demo", "phases": [], "items": []})
    monkeypatch.setattr(
        "harness.epic.run_epic",
        lambda **k: {
            "totals": {"approved_not_landed": 1},
            "quarantined": [],
            "exit_summary": ["approved but not landed: t1-probe — cox runs recover epic-1 t1-probe --repo /repo"],
        },
    )

    profile = tmp_path / "provider.yaml"
    profile.write_text("provider: acme\n", encoding="utf-8")

    exit_code = cli.main(
        [
            "epic", "--team", "acme", "--unverified-skills",
            "--initiative", str(tmp_path / "initiative"), "--repo", "/repo",
            "--run-id", "epic-1", "--runs-dir", str(tmp_path / "runs"),
            "--provider-profile", str(profile),
        ]
    )

    assert exit_code == 0
    err = capsys.readouterr().err
    assert "approved but not landed: t1-probe — cox runs recover epic-1 t1-probe --repo /repo" in err


# ── repo-declared checks, from a root `.agent-checks` file ──────────────────


def test_a_repo_declared_check_runs_alongside_the_cartridges(repo, cart, tmp_path) -> None:
    cmd = f'{sys.executable} -c "pass"'
    (repo / ".agent-checks").write_text(cmd + "\n", encoding="utf-8")
    result, _ = drive(repo, cart, tmp_path, work=initiative(two_phases=False))
    landed = next(t for t in result["tasks"] if t["id"] == "t1-probe")
    seen = {row["check"] for row in landed["evidence"]}
    assert "checks:state" in seen  # the cartridge's own check still ran
    assert f"checks:{sys.executable}" in seen  # and the repo's is merged in beside it


def test_a_bom_prefixed_agent_checks_file_does_not_mangle_the_command(repo, cart, tmp_path) -> None:
    """A BOM would otherwise land inside the first name and cmd, and no shell resolves it."""
    cmd = f'{sys.executable} -c "pass"'
    (repo / ".agent-checks").write_bytes(b"\xef\xbb\xbf" + (cmd + "\n").encode("utf-8"))
    result, _ = drive(repo, cart, tmp_path, work=initiative(two_phases=False))
    landed = next(t for t in result["tasks"] if t["id"] == "t1-probe")
    assert landed["status"] == "built"
    assert f"checks:{sys.executable}" in {row["check"] for row in landed["evidence"]}


def test_an_undecodable_agent_checks_file_falls_back_to_no_repo_checks(repo, cart, tmp_path) -> None:
    """A malformed file degrades to no repo checks, never to a run that dies on it."""
    (repo / ".agent-checks").write_bytes(b"\xff\xfe\x00bad")
    result, _ = drive(repo, cart, tmp_path, work=initiative(two_phases=False))
    landed = next(t for t in result["tasks"] if t["id"] == "t1-probe")
    assert landed["status"] == "built"


def test_a_failing_repo_declared_check_quarantines_naming_it(repo, cart, tmp_path) -> None:
    (repo / ".agent-checks").write_text("false\n", encoding="utf-8")
    runner = Runner(
        {"t1-probe": new_file_patch("t1-probe.txt"), "t2-bench": new_file_patch("t2-bench.txt")},
        verdicts={"p1-foundations": GOAL_UNMET},
    )
    result, _ = drive(repo, cart, tmp_path, runner=runner, work=initiative(two_phases=False))
    quarantined = result["quarantined"]
    reason = next(q["reason"] for q in quarantined if q["id"] == "t1-probe")
    assert "configured check failed" in reason and "false" in reason


def test_a_build_budget_under_the_cap_reaches_the_build_call(repo, cart, tmp_path) -> None:
    cart["policy"]["build_budget_usd_max"] = 3.0
    work = initiative(two_phases=False)
    next(i for i in work["items"] if i["id"] == "t1-probe")["budget_usd"] = 2.0
    result, runner = drive(repo, cart, tmp_path, work=work)
    build_calls = [c for c in runner.calls if c["role"] == "build" and "t1-probe" in c["prompt"]]
    assert build_calls and build_calls[0]["budget_usd"] == 2.0
    assert not any(q["id"] == "t1-probe" for q in result["quarantined"])


def test_a_build_budget_over_the_cap_is_quarantined_and_the_sibling_still_lands(repo, cart, tmp_path) -> None:
    cart["policy"]["build_budget_usd_max"] = 3.0
    work = initiative(two_phases=False)
    next(i for i in work["items"] if i["id"] == "t1-probe")["budget_usd"] = 5.0
    result, runner = drive(repo, cart, tmp_path, work=work)
    reason = next(q["reason"] for q in result["quarantined"] if q["id"] == "t1-probe")
    assert "budget_usd 5.0 exceeds the cartridge cap build_budget_usd_max 3.0" in reason
    assert not any(c["role"] == "build" and "t1-probe" in c["prompt"] for c in runner.calls)
    assert is_ancestor(repo, "epic/demo-initiative/p1-foundations--t2-bench", "epic/demo-initiative/p1-foundations")


def test_a_repo_without_the_file_gets_only_the_cartridges_checks(repo, cart) -> None:
    from harness.epic import _Ctx

    ctx = _Ctx(
        repo=repo, cartridge=cart, runner=None, specs={}, run_id="r", date="d",
        max_parallel=1, ledger_path=Path("/tmp/ledger.jsonl"), provider_profile="p",
        runs_dir=Path("/tmp/runs"), worktree_root=Path("/tmp/worktrees"), assume=None,
        fix_attempts=None, initiative_id="i", default_ref="HEAD",
    )
    assert ctx.checks == cart["landing_areas"]["checks"]


def test_a_task_the_lifecycle_could_not_run_is_quarantined_not_fatal(repo, cart, tmp_path) -> None:
    runner = Runner({"t2-bench": new_file_patch("t2-bench.txt")}, verdicts={"p1-foundations": GOAL_UNMET})
    result, _ = drive(repo, cart, tmp_path, runner=runner, work=initiative(two_phases=False))
    assert [q["id"] for q in result["quarantined"]] == ["t1-probe"]
    assert is_ancestor(repo, "epic/demo-initiative/p1-foundations--t2-bench", "epic/demo-initiative/p1-foundations")


def test_an_unsatisfied_chunk_verdict_quarantines_before_the_gate(repo, cart, tmp_path) -> None:
    runner = Runner(
        {t: new_file_patch(f"{t}.txt") for t in TASK_IDS},
        chunk={"t1-probe": CHUNK_BAD},
        verdicts={"p1-foundations": GOAL_UNMET},
    )
    result, _ = drive(repo, cart, tmp_path, runner=runner, work=initiative(two_phases=False))

    assert [q["id"] for q in result["quarantined"]] == ["t1-probe"]
    assert "validate_chunk unsatisfied" in result["quarantined"][0]["reason"]
    assert result["quarantined"][0]["kind"] == "unverified"
    assert result["quarantined"][0]["patch_kept"] is True
    # No merge was even proposed for it: a task the validator says did not do
    # what it said is not a task whose merge should be up for a decision.
    assert not any("t1-probe" in p["target"] for p in result["proposals"] if p["kind"] == "merge_stack")
    assert "epic/demo-initiative/p1-foundations--t1-probe" not in branches(repo)


def test_an_unverified_quarantine_keeps_the_patch_and_records_it_kept(repo, cart, tmp_path) -> None:
    """`validate_chunk` unsatisfied is `unverified`: the patch stays, on both records."""
    wi = tmp_path / "wi"
    (wi / "p1-foundations").mkdir(parents=True)
    (wi / "initiative.md").write_text(
        "---\nid: demo-initiative\ntitle: demo\n---\n\nmake the vendor join measurable end to end\n"
    )
    (wi / "p1-foundations" / "t1-probe.md").write_text(
        "---\nid: t1-probe\nphase: p1-foundations\nstate: ready\nneeds: []\nsurfaces: []\n"
        "title: schema probe\n---\n\nread the vendor schema\n"
    )
    work = workstore.read_initiative(wi)

    runner = Runner({"t1-probe": new_file_patch("t1-probe.txt")}, chunk={"t1-probe": CHUNK_BAD})
    result, _ = drive(repo, cart, tmp_path, runner=runner, work=work, run_id="epic-unverified")

    entry = next(q for q in result["quarantined"] if q["id"] == "t1-probe")
    assert entry["kind"] == "unverified"
    assert entry["patch_kept"] is True

    item = workstore.read_item(wi / "p1-foundations" / "t1-probe.md")
    attempt = item["attempts"][0]
    assert attempt["kind"] == "unverified"
    assert attempt["patch_kept"] is True


def test_a_worktree_that_cannot_be_opened_fails_the_phase_not_a_task(repo, cart, tmp_path, monkeypatch) -> None:
    """`infra` is the caller's own branch: no task-grain entry, the phase itself is marked."""
    monkeypatch.setattr("harness.epic._open_phase_worktree", lambda ctx, phase, base_ref: (False, "boom", False))
    result, _ = drive(repo, cart, tmp_path, work=initiative(two_phases=False))

    p1 = result["phases"][0]
    assert p1["status"] == "blocked"
    assert p1["phase_failed_to_start"] == p1["reason"]
    assert "boom" in p1["phase_failed_to_start"]
    assert not any(q["grain"] == "task" for q in result["quarantined"])


def test_open_phase_worktree_prunes_a_stale_registration_before_opening(repo, cart, tmp_path) -> None:
    """A crashed prior run's leftover admin entry must not block the next open.

    2026-09-08 `tools-chair-rename-18`: the worktree directory was gone but the
    registration was not, and the next `worktree add` failed on the phantom.
    """
    import shutil

    from harness.epic import _Ctx, _open_phase_worktree

    ctx = _Ctx(
        repo=repo, cartridge=cart, runner=None, specs={}, run_id="r", date="d",
        max_parallel=1, ledger_path=tmp_path / "ledger.jsonl", provider_profile="p",
        runs_dir=tmp_path / "runs", worktree_root=tmp_path / "worktrees", assume=None,
        fix_attempts=None, initiative_id="demo-initiative", default_ref="main",
    )
    ok, detail, _ = _open_phase_worktree(ctx, "p1-foundations", "main")
    assert ok, detail
    shutil.rmtree(ctx.phase_worktree("p1-foundations"))

    ok, detail, reused = _open_phase_worktree(ctx, "p1-foundations", "main")
    assert ok, detail
    assert reused is True


# ── governance ──────────────────────────────────────────────────────────────


def test_a_governance_patch_cannot_earn_its_merge(repo, cart, tmp_path) -> None:
    """Escalation reaches the MERGE too, or the escalation is decoration."""
    runner = Runner(
        {
            "t1-probe": new_file_patch("cartridges/x.yaml", "ok"),
            "t2-bench": new_file_patch("t2-bench.txt"),
        }
    )
    result, _ = drive(repo, cart, tmp_path, runner=runner, work=initiative(two_phases=False))

    escalated = [p for p in result["proposals"] if p["kind"] == "self_modification"]
    assert {p.get("escalated_from") for p in escalated} == {"draft_pr_create", "merge_stack"}
    assert not any("t1-probe" in p["target"] for p in result["proposals"] if p["kind"] == "merge_stack")

    # Approved at the gate and still not executed: the arm is `pr`, and there is
    # no path in the driver that merges a governance change.
    assert "epic/demo-initiative/p1-foundations--t1-probe" not in branches(repo)
    assert not is_ancestor(repo, "agents/epic-1/t1-probe", "epic/demo-initiative/p1-foundations")
    assert is_ancestor(repo, "epic/demo-initiative/p1-foundations--t2-bench", "epic/demo-initiative/p1-foundations")

    rows = [row for row in ledger.read(tmp_path / "ledger.jsonl") if row["kind"] == "self_modification"]
    assert {row["outcome"] for row in rows} == {"skipped"}, "approved, never executed — neither win nor reversal"


def test_an_escalated_tasks_state_move_lands_on_approved_not_done(repo, cart, tmp_path) -> None:
    """A merge that was never applied cannot read `done` — only `approved`."""
    runner = Runner(
        {
            "t1-probe": new_file_patch("harness/x.py", "ok"),
            "t2-bench": new_file_patch("t2-bench.txt"),
        }
    )
    result, _ = drive(repo, cart, tmp_path, runner=runner, work=initiative(two_phases=False))

    escalated = next(t for t in result["tasks"] if t["id"] == "t1-probe")
    assert escalated["merged"] is False
    assert escalated["outcome"] == "approved_not_landed"
    assert escalated["status"] == "approved"

    landed = next(t for t in result["tasks"] if t["id"] == "t2-bench")
    assert landed["merged"] is True
    assert landed["outcome"] == "landed"


def test_a_task_outside_governance_paths_still_lands_on_done(repo, cart, tmp_path) -> None:
    """The new escalated-only branch leaves a normally-merged task's record alone."""
    result, _ = drive(repo, cart, tmp_path, work=initiative(two_phases=False))

    task = next(t for t in result["tasks"] if t["id"] == "t1-probe")
    assert task["merged"] is True
    assert task["outcome"] == "landed"
    assert task["status"] == "built"


def test_a_dependent_phase_stays_blocked_on_an_escalated_parent_in_the_same_run(repo, cart, tmp_path) -> None:
    """`approved`, never `done`, is what keeps a dependent from reading its parent as ready."""
    runner = Runner(
        {
            "t1-probe": new_file_patch("harness/x.py", "ok"),
            "t2-bench": new_file_patch("t2-bench.txt"),
            "t3-cutover": new_file_patch("t3-cutover.txt"),
        }
    )
    result, _ = drive(repo, cart, tmp_path, runner=runner, work=initiative(two_phases=True))

    p1, p2 = result["phases"]
    assert p1["status"] == "partial"
    assert p2["status"] == "blocked"
    assert "did not meet its goal" in p2["reason"]
    assert not any(c["role"] == "build" and "t3-cutover" in c["prompt"] for c in runner.calls), (
        "p2 never ran, so its own task was never built off ground p1 never actually landed"
    )

    escalated = next(t for t in result["tasks"] if t["id"] == "t1-probe")
    assert escalated["status"] == "approved"
    assert escalated["merged"] is False


# ── the branch-action decision, on literals ─────────────────────────────────


def test_branch_action_recreates_or_blocks_only_a_stale_reused_branch() -> None:
    assert branch_action(reused=False, head_moved=True, has_own_commits=True) == "proceed"
    assert branch_action(reused=True, head_moved=False, has_own_commits=True) == "proceed"
    assert branch_action(reused=True, head_moved=True, has_own_commits=False) == "recreate"
    assert branch_action(reused=True, head_moved=True, has_own_commits=True) == "block"


# ── re-entrancy: recreate, block, and the stack rebase ──────────────────────


def test_no_rebase_is_proposed_when_the_stack_is_still_on_its_base(repo, cart, tmp_path) -> None:
    work = initiative(two_phases=False)
    drive(repo, cart, tmp_path, work=work, run_id="epic-1")
    done = initiative(two_phases=False, done=("t1-probe", "t2-bench"))
    second, _ = drive(repo, cart, tmp_path, work=done, run_id="epic-2")
    assert [p for p in second["proposals"] if p["kind"] == "stack_rebase"] == []
    assert second["totals"]["stacks_rebased"] == 0
    assert second["phases"][0]["status"] == "complete"


# ── `dropped` is terminal, the same way `done` is ───────────────────────────


def test_a_dropped_task_is_never_selected_for_build_or_review(repo, cart, tmp_path) -> None:
    work = initiative(two_phases=False)
    work["items"][1]["state"] = "dropped"
    result, runner = drive(repo, cart, tmp_path, work=work)
    assert not any(c["role"] == "build" and "t2-bench" in c["prompt"] for c in runner.calls)
    assert result["phases"][0]["status"] == "complete"


def test_a_phase_of_done_and_dropped_records_is_complete(repo, cart, tmp_path) -> None:
    work = initiative(two_phases=False, done=("t1-probe",))
    work["items"][1]["state"] = "dropped"
    result, runner = drive(repo, cart, tmp_path, work=work)
    assert result["phases"][0]["status"] == "complete"
    assert runner.calls == [], "every item was already done or dropped — nothing to build or review"


def test_a_stale_empty_reused_branch_is_recreated_before_any_task_builds(repo, cart, tmp_path) -> None:
    """Refused day one leaves the branch equal to its base — nothing of its own to lose."""
    work = initiative(two_phases=False)
    first, _ = drive(repo, cart, tmp_path, work=work, run_id="epic-1", assume="r")
    assert first["phases"][0]["status"] == "partial"
    assert git("rev-parse", "epic/demo-initiative/p1-foundations", cwd=repo) == git("rev-parse", "main", cwd=repo)

    (repo / "moved.md").write_text("moved\n", encoding="utf-8")
    git("add", "-A", cwd=repo)
    git("commit", "-qm", "advance main", cwd=repo)
    assert not is_ancestor(repo, "main", "epic/demo-initiative/p1-foundations")

    second, _ = drive(repo, cart, tmp_path, work=work, run_id="epic-2")
    assert second["phases"][0]["status"] == "complete"
    assert is_ancestor(repo, "main", "epic/demo-initiative/p1-foundations")
    assert [p for p in second["proposals"] if p["kind"] == "stack_rebase"] == []


def test_a_stale_reused_branch_with_its_own_commits_blocks_rather_than_building(repo, cart, tmp_path) -> None:
    work = initiative(two_phases=False)
    first, _ = drive(repo, cart, tmp_path, work=work, run_id="epic-1")
    assert first["phases"][0]["status"] == "complete"
    before = git("rev-parse", "epic/demo-initiative/p1-foundations", cwd=repo)

    (repo / "moved.md").write_text("moved\n", encoding="utf-8")
    git("add", "-A", cwd=repo)
    git("commit", "-qm", "advance main", cwd=repo)

    second, runner = drive(repo, cart, tmp_path, work=work, run_id="epic-2")
    assert second["phases"][0]["status"] == "blocked"
    reason = second["phases"][0]["reason"]
    assert "epic/demo-initiative/p1-foundations" in reason and "main" in reason
    assert not any(c["role"] == "build" for c in runner.calls)
    assert git("rev-parse", "epic/demo-initiative/p1-foundations", cwd=repo) == before

    # The gate has something to decide on: the same kind of proposal the
    # quiet path files, not a phase quarantined with nothing to act on.
    rebases = [p for p in second["proposals"] if p["kind"] == "stack_rebase"]
    assert len(rebases) == 1 and rebases[0]["target"] == "epic/demo-initiative/p1-foundations"

    # The branch is released, not wedged: a third run can open it again.
    third, _ = drive(repo, cart, tmp_path, work=work, run_id="epic-3")
    assert third["phases"][0]["status"] == "blocked"
    assert "rebase it through the gate" in third["phases"][0]["reason"]


def test_a_stale_branch_whose_diff_adds_no_lines_is_recreated_not_blocked(repo, cart, tmp_path) -> None:
    """Two of its own commits, net zero lines — old-logic 'has commits' would have blocked this."""
    branch = "epic/demo-initiative/p1-foundations"
    git("checkout", "-b", branch, cwd=repo)
    (repo / "temp.txt").write_text("x\n", encoding="utf-8")
    git("add", "-A", cwd=repo)
    git("commit", "-qm", "add temp", cwd=repo)
    (repo / "temp.txt").unlink()
    git("add", "-A", cwd=repo)
    git("commit", "-qm", "remove temp", cwd=repo)
    git("checkout", "main", cwd=repo)

    (repo / "moved.md").write_text("moved\n", encoding="utf-8")
    git("add", "-A", cwd=repo)
    git("commit", "-qm", "advance main", cwd=repo)

    result, _ = drive(repo, cart, tmp_path, work=initiative(two_phases=False), run_id="epic-2")
    assert result["phases"][0]["status"] == "complete"
    assert "recreated" in result["phases"][0]


def test_a_stale_branch_whose_diff_adds_a_line_still_blocks(repo, cart, tmp_path) -> None:
    branch = "epic/demo-initiative/p1-foundations"
    git("checkout", "-b", branch, cwd=repo)
    (repo / "keep.txt").write_text("kept\n", encoding="utf-8")
    git("add", "-A", cwd=repo)
    git("commit", "-qm", "add keep", cwd=repo)
    git("checkout", "main", cwd=repo)

    (repo / "moved.md").write_text("moved\n", encoding="utf-8")
    git("add", "-A", cwd=repo)
    git("commit", "-qm", "advance main", cwd=repo)

    result, runner = drive(repo, cart, tmp_path, work=initiative(two_phases=False), run_id="epic-2")
    assert result["phases"][0]["status"] == "blocked"
    assert not any(c["role"] == "build" for c in runner.calls)


class Revising(Runner):
    """Reviews everything as `revise`, so the fix loop is the only thing running."""

    def run(self, *, role, tier, schema, prompt, context=(), thread=None, budget_usd=None, task=None):
        if role == "review_charter":
            with self.lock:
                self.calls.append({"role": role, "tier": tier, "prompt": prompt})
            return {"verdict": "revise", "findings": [], "rationale": "not yet"}
        return super().run(role=role, tier=tier, schema=schema, prompt=prompt, context=context, budget_usd=budget_usd)


def builds_per_task(runner) -> dict[str, int]:
    counts: dict[str, int] = {}
    for call in runner.calls:
        if call["role"] == "build":
            task = next(t for t in TASK_IDS if t in call["prompt"])
            counts[task] = counts.get(task, 0) + 1
    return counts


def test_fix_attempts_reaches_the_lifecycle_graph(repo, cart, tmp_path) -> None:
    """The driver's one knob over the loop, and the only way to see it is to count."""
    work = initiative(two_phases=False)
    patches = {t: new_file_patch(f"{t}.txt") for t in TASK_IDS}

    _, capped = drive(
        repo, cart, tmp_path, runner=Revising(patches, verdicts={"p1-foundations": GOAL_UNMET}),
        work=work, fix_attempts=0,
    )
    assert set(builds_per_task(capped).values()) == {1}, "fix_attempts=0 must not retry"

    _, default = drive(
        repo, cart, tmp_path, runner=Revising(patches, verdicts={"p1-foundations": GOAL_UNMET}),
        work=work, run_id="epic-2",
    )
    assert set(builds_per_task(default).values()) == {2}, "the default loop retries once, then sees no progress"


# ── the autonomy seam ───────────────────────────────────────────────────────


def seed(path: Path, kind: str, risk: str, n: int, outcome: str = "clean") -> None:
    ledger.append(
        [
            {
                "run_id": f"seed-{i}",
                "ts": "2026-08-30T00:00:00Z",
                "principal": "epic-swarm(lifecycle-propose)",
                "kind": kind,
                "risk": risk,
                "outcome": outcome,
                "cartridge_sha": SHA,
                "provider_profile": PROFILE,
            }
            for i in range(n)
        ],
        path,
    )


def test_a_graduated_merge_auto_applies_and_records_no_ledger_row(repo, cart, tmp_path) -> None:
    """Autonomy is spent by acting: an auto-apply never reached a gate."""
    path = tmp_path / "ledger.jsonl"
    seed(path, "merge_stack", "high", 3)
    before = len(ledger.read(path))

    result, _ = drive(repo, cart, tmp_path, work=initiative(two_phases=False))

    rows = ledger.read(path)
    assert [row for row in rows[before:] if row["kind"] == "merge_stack"] == [], (
        "an auto-applied merge must not extend its own streak"
    )
    # It really did merge, without a human and without a row.
    assert all(task["merged"] for task in result["tasks"])
    assert result["phases"][0]["totals"]["auto_applied"] == 2


# ── determinism ─────────────────────────────────────────────────────────────


def test_proposals_and_tasks_come_back_in_task_id_order(repo, cart, tmp_path) -> None:
    result, _ = drive(repo, cart, tmp_path)
    assert [task["id"] for task in result["tasks"]] == ["t1-probe", "t2-bench", "t3-cutover"]

    merges = [p["target"] for p in result["proposals"] if p["kind"] == "merge_stack"]
    assert merges == [
        "epic/demo-initiative/p1-foundations--t1-probe -> epic/demo-initiative/p1-foundations",
        "epic/demo-initiative/p1-foundations--t2-bench -> epic/demo-initiative/p1-foundations",
        "epic/demo-initiative/p2-rollout--t3-cutover -> epic/demo-initiative/p2-rollout",
    ]
    moves = [p["target"] for p in result["proposals"] if p["kind"] == "state_move"]
    assert moves == ["t1-probe", "t2-bench", "t3-cutover"]


def test_two_runs_over_the_same_work_propose_the_same_things(repo, cart, tmp_path) -> None:
    first, _ = drive(repo, cart, tmp_path, assume="r", run_id="epic-1")
    second, _ = drive(repo, cart, tmp_path, assume="r", run_id="epic-2")
    shape = lambda result: [(p["kind"], p["target"].replace("epic-2", "epic-1")) for p in result["proposals"]]  # noqa: E731
    assert shape(first) == shape(second)


# ── §4 consolidate ────────────────────────────────────────────────────────


def _two_task_initiative(*, t2_surfaces: list[str]) -> dict:
    """t1-probe and t2-bench, one phase, no dependency between them.

    `surfaces` is the field `docs/design/work-shape.md` §3's coupling rule
    already reads to decide who owns what; §4's ownership check reuses it.
    """
    items = [
        {"id": "t1-probe", "phase": "p1-foundations", "state": "ready", "needs": [], "surfaces": [],
         "title": "schema probe", "body": "read the vendor schema"},
        {"id": "t2-bench", "phase": "p1-foundations", "state": "ready", "needs": [], "surfaces": t2_surfaces,
         "title": "bench harness", "body": "time the join"},
    ]
    return {
        "id": "demo-initiative", "title": "demo", "body": "make the vendor join measurable end to end",
        "phases": ["p1-foundations"], "items": items,
    }


def test_a_finding_citing_a_file_the_other_task_owns_emits_one_consolidate_proposal(
    repo, cart, tmp_path
) -> None:
    patches = {t: new_file_patch(f"{t}.txt") for t in ("t1-probe", "t2-bench")}
    # Ordinary reviewer prose, not the taxonomy's own words: no "cannot satisfy",
    # no "without touching", no literal "t2-bench" id — only the required `file`
    # field naming a surface t2-bench actually claims.
    blocked = {
        "verdict": "approve",
        "findings": [
            {
                "charter_principle": "cross-ticket reach",
                "detail": "Landing this cleanly also needs an edit here, and that belongs to the bench ticket.",
                "file": "t2-bench.txt",
            }
        ],
        "rationale": "the patch is fine on its own terms",
    }
    runner = Runner(patches, review={"t1-probe": blocked})
    work = _two_task_initiative(t2_surfaces=["t2-bench.txt"])
    result, _ = drive(repo, cart, tmp_path, work=work, runner=runner)

    consolidate = [p for p in result["proposals"] if p["kind"] == "consolidate"]
    assert len(consolidate) == 1
    assert consolidate[0]["target"] == "t1-probe and t2-bench"
    assert consolidate[0]["evidence"][0]["output"] == blocked["findings"][0]["detail"]


def test_a_run_with_no_such_finding_emits_no_consolidate_proposal(repo, cart, tmp_path) -> None:
    # t2-bench owns t2-bench.txt, same as the positive case, but nothing t1-probe's
    # review says ever names it: ownership alone never fires the proposal.
    work = _two_task_initiative(t2_surfaces=["t2-bench.txt"])
    result, _ = drive(repo, cart, tmp_path, work=work)
    assert [p for p in result["proposals"] if p["kind"] == "consolidate"] == []


# ── a build the fix loop refused never reaches a validator ──────────────────

REVISE = {
    "verdict": "revise",
    "findings": [{"charter_principle": "evidence", "detail": "the status line does not match the code", "file": "t1-probe.txt"}],
    "rationale": "the claim and the code disagree",
}


class RefusedRunner(Runner):
    """One task whose reviewer says revise and whose fix build changes nothing.

    The builder returns the same patch on the retry — which is what a real
    no-progress fix build does — so `lifecycle-propose` stops the loop with
    `no_progress`, emits no `draft_pr_create`, and leaves `review.verdict` at
    `revise`. That is the exact shape of the record this driver used to apply,
    check, and then hand to a chunk validator as though the verdict were live.
    """

    def __init__(self, patches: dict[str, str], *, refused: str, **kw) -> None:
        super().__init__(patches, **kw)
        self.refused = refused

    def run(self, *, role, tier, schema, prompt, context=(), thread=None, budget_usd=None, task=None):
        if role == "review_charter" and self.refused in prompt:
            with self.lock:
                self.calls.append({"role": role, "tier": tier, "prompt": prompt})
            return dict(REVISE)
        return super().run(role=role, tier=tier, schema=schema, prompt=prompt, context=context, thread=thread, budget_usd=budget_usd)


def test_a_build_the_fix_loop_refused_is_quarantined_with_the_loop_s_own_reason(repo, cart, tmp_path) -> None:
    runner = RefusedRunner(
        {t: new_file_patch(f"{t}.txt") for t in TASK_IDS}, refused="t1-probe"
    )
    result, runner = drive(repo, cart, tmp_path, runner=runner, work=initiative(two_phases=False))

    quarantined = [q for q in result["quarantined"] if q["grain"] == "task"]
    assert [q["id"] for q in quarantined] == ["t1-probe"]
    reason = quarantined[0]["reason"]
    assert quarantined[0]["kind"] == "refused"

    # The loop's diagnosis, not a validator's restatement of a stale verdict.
    assert "no_progress" in reason, reason
    assert "validate_chunk" not in reason, reason
    assert "'revise'" in reason, reason


# ── a node failure mid-review writes the task record on the way out ────────


class ArbitrateFailsRunner(Runner):
    """Charter and adversary both answer; arbitrate then raises — the exact
    shape the ticket names: build complete, charter review ran, then
    arbitrate failed with a provider-side error."""

    def run(self, *, role, tier, schema, prompt, context=(), thread=None, budget_usd=None, task=None):
        if role == "review_adversary":
            with self.lock:
                self.calls.append({"role": role, "tier": tier, "prompt": prompt})
            return dict(APPROVE)
        if role == "arbitrate":
            raise RunnerError("provider-side safeguard error")
        return super().run(role=role, tier=tier, schema=schema, prompt=prompt, context=context, thread=thread, budget_usd=budget_usd)


def test_a_non_build_node_failure_quarantines_as_infra_with_the_patch_kept(repo, cart, tmp_path) -> None:
    cart = dict(cart)
    cart["skills"] = {**cart["skills"], "review_adversary": "acme-skills:review-adversary", "arbitrate": "acme-skills:arbitrate"}
    cart["policy"] = {**cart["policy"], "review_tier": {"tier2_surfaces": ["dangerous"]}}
    work = initiative(two_phases=False)
    work["items"][0]["surfaces"] = ["dangerous"]
    runner = ArbitrateFailsRunner({t: new_file_patch(f"{t}.txt") for t in TASK_IDS})
    result, _ = drive(repo, cart, tmp_path, runner=runner, work=work, run_id="epic-infra")

    quarantined = [q for q in result["quarantined"] if q["grain"] == "task"]
    assert [q["id"] for q in quarantined] == ["t1-probe"]
    assert quarantined[0]["kind"] == "infra"
    assert quarantined[0]["patch_kept"] is True
    assert "arbitrate" in quarantined[0]["reason"]

    task_record = next(t for t in result["tasks"] if t["id"] == "t1-probe")
    # Nothing was judged, so this is a harness fault, never "rejected" — the
    # label a patch two reviewers actually declined gets.
    assert task_record["outcome"] == "harness_fault"

    saved = load_result(tmp_path / "runs", "epic-infra", "p1-foundations", "t1-probe")
    assert saved is not None
    assert saved["build"]["patch"].strip()
    assert saved["failed_node"] == "arbitrate"


class ArbitrateBudgetStopRunner(Runner):
    """arbitrate hits its budget ceiling — a resumable stop, not a node
    failure, so it must NOT convert to `infra`; it falls through to the
    pre-existing `no_work` path exactly like any other `RunnerError` from a
    non-build node did before this ticket."""

    def run(self, *, role, tier, schema, prompt, context=(), thread=None, budget_usd=None, task=None):
        if role == "review_adversary":
            with self.lock:
                self.calls.append({"role": role, "tier": tier, "prompt": prompt})
            return dict(APPROVE)
        if role == "arbitrate":
            raise BudgetStop(role="arbitrate", thread=None, session=None, spent_usd=1.0, detail="budget ceiling hit")
        return super().run(role=role, tier=tier, schema=schema, prompt=prompt, context=context, thread=thread, budget_usd=budget_usd)


def test_a_budget_stop_from_a_non_build_node_is_not_converted_to_infra(repo, cart, tmp_path) -> None:
    cart = dict(cart)
    cart["skills"] = {**cart["skills"], "review_adversary": "acme-skills:review-adversary", "arbitrate": "acme-skills:arbitrate"}
    cart["policy"] = {**cart["policy"], "review_tier": {"tier2_surfaces": ["dangerous"]}}
    work = initiative(two_phases=False)
    work["items"][0]["surfaces"] = ["dangerous"]
    runner = ArbitrateBudgetStopRunner({t: new_file_patch(f"{t}.txt") for t in TASK_IDS})
    result, _ = drive(repo, cart, tmp_path, runner=runner, work=work, run_id="epic-budget-arb")

    quarantined = [q for q in result["quarantined"] if q["grain"] == "task"]
    assert [q["id"] for q in quarantined] == ["t1-probe"]
    assert quarantined[0]["kind"] == "no_work"
    assert "patch_kept" not in quarantined[0]
    assert "t1-probe" not in [t["id"] for t in result["tasks"]]


class AdversaryFailsRunner(Runner):
    """Charter answers; the adversary then raises for one task only. The
    charter's own verdict was already in hand when the adversary node
    failed, so it must survive onto the saved result rather than being
    dropped with the traceback."""

    def __init__(self, patches: dict[str, str], *, fails_for: str, **kw) -> None:
        super().__init__(patches, **kw)
        self.fails_for = fails_for

    def run(self, *, role, tier, schema, prompt, context=(), thread=None, budget_usd=None, task=None):
        if role == "review_adversary":
            if self.fails_for in prompt:
                raise RunnerError("provider-side safeguard error")
            return dict(APPROVE)
        return super().run(role=role, tier=tier, schema=schema, prompt=prompt, context=context, thread=thread, budget_usd=budget_usd)


def test_the_charter_verdict_already_in_hand_survives_an_adversary_failure(repo, cart, tmp_path) -> None:
    # tier1_max_changed_lines defaults to 150, so every small fixture patch
    # already sits at tier 1 — binding review_adversary is enough to call it.
    cart = dict(cart)
    cart["skills"] = {**cart["skills"], "review_adversary": "acme-skills:review-adversary"}
    runner = AdversaryFailsRunner({t: new_file_patch(f"{t}.txt") for t in TASK_IDS}, fails_for="t1-probe")
    result, _ = drive(repo, cart, tmp_path, runner=runner, work=initiative(two_phases=False), run_id="epic-adv-infra")

    quarantined = [q for q in result["quarantined"] if q["grain"] == "task"]
    assert [q["id"] for q in quarantined] == ["t1-probe"]
    assert quarantined[0]["kind"] == "infra"
    assert "review_adversary" in quarantined[0]["reason"]

    saved = load_result(tmp_path / "runs", "epic-adv-infra", "p1-foundations", "t1-probe")
    assert saved is not None
    assert saved["failed_node"] == "review_adversary"
    assert saved["review"]["verdict"] == "approve"


def test_a_build_node_failure_is_still_quarantined_as_no_work(repo, cart, tmp_path) -> None:
    """Unchanged: a build that never produced a patch has nothing to keep."""
    runner = Runner({"t2-bench": new_file_patch("t2-bench.txt")})
    result, _ = drive(repo, cart, tmp_path, runner=runner, work=initiative(two_phases=False))

    quarantined = [q for q in result["quarantined"] if q["grain"] == "task"]
    assert [q["id"] for q in quarantined] == ["t1-probe"]
    assert quarantined[0]["kind"] == "no_work"
    assert "patch_kept" not in quarantined[0]

    assert "t1-probe" not in [t["id"] for t in result["tasks"]]


def test_a_quarantined_task_records_an_attempt_on_its_own_work_item(repo, cart, tmp_path) -> None:
    """The item file, not just the phase record, remembers the quarantine."""
    wi = tmp_path / "wi"
    (wi / "p1-foundations").mkdir(parents=True)
    (wi / "initiative.md").write_text(
        "---\nid: demo-initiative\ntitle: demo\n---\n\nmake the vendor join measurable end to end\n"
    )
    (wi / "p1-foundations" / "t1-probe.md").write_text(
        "---\nid: t1-probe\nphase: p1-foundations\nstate: ready\nneeds: []\nsurfaces: []\n"
        "title: schema probe\n---\n\nread the vendor schema\n"
    )
    (wi / "p1-foundations" / "t2-bench.md").write_text(
        "---\nid: t2-bench\nphase: p1-foundations\nstate: ready\nneeds: []\nsurfaces: []\n"
        "title: bench harness\n---\n\ntime the join\n"
    )
    work = workstore.read_initiative(wi)

    # No patch scripted for t2-bench, so its build raises and it is quarantined
    # by the `invoke_graphs` failure loop — a different call site from t1-probe's
    # fix-loop refusal, and both tasks are file-backed so both writes are live.
    runner = RefusedRunner({"t1-probe": new_file_patch("t1-probe.txt")}, refused="t1-probe")
    result, _ = drive(repo, cart, tmp_path, runner=runner, work=work, run_id="epic-attempt")

    quarantined = {q["id"]: q for q in result["quarantined"] if q["grain"] == "task"}
    assert set(quarantined) == {"t1-probe", "t2-bench"}

    # t1-probe: the fix loop's own refusal. t2-bench: no build was ever
    # scripted for it, so `invoke_graphs` reports it as a swarm failure.
    expected_kind = {"t1-probe": "refused", "t2-bench": "no_work"}
    for task in ("t1-probe", "t2-bench"):
        entry = quarantined[task]
        assert set(entry) == {"id", "phase", "grain", "reason", "kind"}
        assert entry["kind"] == expected_kind[task]
        item = workstore.read_item(wi / "p1-foundations" / f"{task}.md")
        assert len(item["attempts"]) == 1
        attempt = item["attempts"][0]
        assert attempt["run"] == "epic-attempt"
        assert attempt["phase"] == "p1-foundations"
        assert attempt["reason"] == entry["reason"]
        assert attempt["kind"] == expected_kind[task]
        assert attempt["ts"]

    # The round trip that matters: re-reading the store (not a hand-built dict)
    # after the write, so the second run's task dicts are whatever
    # `read_initiative` actually produces, attempts included.
    work2 = workstore.read_initiative(wi)
    runner2 = Runner({"t1-probe": new_file_patch("t1-probe.txt"), "t2-bench": new_file_patch("t2-bench.txt")})
    drive(repo, cart, tmp_path, runner=runner2, work=work2, run_id="epic-attempt-2")

    for task in ("t1-probe", "t2-bench"):
        prompt = next(c["prompt"] for c in runner2.calls if c["role"] == "plan" and task in c["prompt"])
        assert quarantined[task]["reason"] in prompt


# ── the previous attempt's reasons and patch, carried into the ticket body ──


def test_a_recorded_attempt_carries_its_reason_and_last_patch_into_both_prompts(repo, cart, tmp_path) -> None:
    """What worked by hand seven times: the reference implementation and the objections, in the body."""
    work = initiative()
    work["items"][0]["attempts"] = [
        {"run": "epic-0", "phase": "p1-foundations", "reason": "checks failed: state 1 failed", "ts": "t"}
    ]
    save_result(
        {"ticket": "t1-probe", "build": {"patch": new_file_patch("t1-probe-old.txt", "distinctive-marker")}},
        runs_dir=tmp_path / "runs", run_id="epic-0", phase="p1-foundations", task="t1-probe",
    )

    result, runner = drive(repo, cart, tmp_path, work=work)

    for role in ("plan", "build"):
        prompt = next(c["prompt"] for c in runner.calls if c["role"] == role and "t1-probe" in c["prompt"])
        assert "checks failed: state 1 failed" in prompt
        assert "distinctive-marker" in prompt
        assert "NOT approved" in prompt

    # The sibling with no attempts carries nothing.
    sibling = next(c["prompt"] for c in runner.calls if c["role"] == "plan" and "t2-bench" in c["prompt"])
    assert "## Previous attempts" not in sibling
    assert "```diff" not in sibling


def test_a_recorded_attempt_with_no_saved_result_still_carries_its_reason(repo, cart, tmp_path) -> None:
    """A run that never got as far as a build has nothing to load — the reason still rides along."""
    work = initiative()
    work["items"][0]["attempts"] = [
        {"run": "epic-missing", "phase": "p1-foundations", "reason": "the fix loop gave up: no_progress", "ts": "t"}
    ]

    result, runner = drive(repo, cart, tmp_path, work=work)

    for role in ("plan", "build"):
        prompt = next(c["prompt"] for c in runner.calls if c["role"] == role and "t1-probe" in c["prompt"])
        assert "the fix loop gave up: no_progress" in prompt
        assert "```diff" not in prompt


def test_a_task_with_no_attempts_is_planned_exactly_as_before(repo, cart, tmp_path) -> None:
    """No history to carry, so the body reaching the planner is unchanged."""
    result, runner = drive(repo, cart, tmp_path)

    prompt = next(c["prompt"] for c in runner.calls if c["role"] == "plan" and "t1-probe" in c["prompt"])
    assert "## Previous attempts" not in prompt
    assert "read the vendor schema" in prompt


def test_a_task_at_the_attempt_cap_is_refused_and_its_sibling_still_lands(repo, cart, tmp_path) -> None:
    """Two recorded attempts and a third run is refused outright, for a person to decide."""
    wi = tmp_path / "wi"
    (wi / "p1-foundations").mkdir(parents=True)
    (wi / "initiative.md").write_text(
        "---\nid: demo-initiative\ntitle: demo\n---\n\nmake the vendor join measurable end to end\n"
    )
    (wi / "p1-foundations" / "t1-probe.md").write_text(
        "---\nid: t1-probe\nphase: p1-foundations\nstate: ready\nneeds: []\nsurfaces: []\n"
        "title: schema probe\n"
        "attempts:\n"
        "  - {run: epic-0, phase: p1-foundations, reason: 'first refusal', ts: '2026-01-01T00:00:00+00:00'}\n"
        "  - {run: epic-1, phase: p1-foundations, reason: 'second refusal', ts: '2026-01-02T00:00:00+00:00'}\n"
        "---\n\nread the vendor schema\n"
    )
    (wi / "p1-foundations" / "t2-bench.md").write_text(
        "---\nid: t2-bench\nphase: p1-foundations\nstate: ready\nneeds: []\nsurfaces: []\n"
        "title: bench harness\n---\n\ntime the join\n"
    )
    work = workstore.read_initiative(wi)

    # `cart` declares no `notify` write kind, so a `genuine_reject` classification's
    # proposal is itself refused — triage runs, but cannot turn this into a write,
    # and the task still ends up quarantined, plainly, for a person to decide.
    class TriageRejectRunner(Runner):
        def run(self, *, role, tier, schema, prompt, context=(), thread=None, budget_usd=None, task=None):
            if role == "triage":
                with self.lock:
                    self.calls.append({"role": role, "tier": tier, "prompt": prompt, "budget_usd": budget_usd})
                return {
                    "class": "genuine_reject",
                    "diagnosis": "the record shows first refusal already happened once",
                    "cites": ["attempt-1|reason"],
                    "action": "no further action",
                }
            return super().run(
                role=role, tier=tier, schema=schema, prompt=prompt, context=context, thread=thread,
                budget_usd=budget_usd, task=task,
            )

    runner = TriageRejectRunner({"t2-bench": new_file_patch("t2-bench.txt")})
    result, _ = drive(
        repo, cart, tmp_path, runner=runner, work=work, run_id="epic-cap",
        specs={**SPECS, "triage-quarantine": triage_quarantine.SPEC},
    )

    assert not any(call["role"] in ("plan", "build") and "t1-probe" in call["prompt"] for call in runner.calls)
    assert any(call["role"] == "triage" for call in runner.calls)

    quarantined = {q["id"]: q for q in result["quarantined"] if q["grain"] == "task"}
    assert set(quarantined) == {"t1-probe"}
    reason = quarantined["t1-probe"]["reason"]
    assert "attempt cap" in reason
    assert "first refusal" in reason
    assert "second refusal" in reason
    # Attempt cap is "nothing produced", not a reviewer's rejection of a
    # patch — docs/design/observed-record.md §3 puts it under `no_work`.
    assert quarantined["t1-probe"]["kind"] == "no_work"

    assert is_ancestor(repo, "epic/demo-initiative/p1-foundations--t2-bench", "epic/demo-initiative/p1-foundations")
    assert "epic/demo-initiative/p1-foundations--t1-probe" not in branches(repo)

    # A refusal past the cap is not itself an attempt: the item still shows
    # only the two earlier ones, neither of which grew a third entry.
    item = workstore.read_item(wi / "p1-foundations" / "t1-probe.md")
    assert len(item["attempts"]) == 2


def test_a_task_with_one_recorded_attempt_still_runs(repo, cart, tmp_path) -> None:
    """One attempt is short of the cap, so the task runs like any other."""
    wi = tmp_path / "wi"
    (wi / "p1-foundations").mkdir(parents=True)
    (wi / "initiative.md").write_text(
        "---\nid: demo-initiative\ntitle: demo\n---\n\nmake the vendor join measurable end to end\n"
    )
    (wi / "p1-foundations" / "t1-probe.md").write_text(
        "---\nid: t1-probe\nphase: p1-foundations\nstate: ready\nneeds: []\nsurfaces: []\n"
        "title: schema probe\n"
        "attempts:\n"
        "  - {run: epic-0, phase: p1-foundations, reason: 'first refusal', ts: '2026-01-01T00:00:00+00:00'}\n"
        "---\n\nread the vendor schema\n"
    )
    work = workstore.read_initiative(wi)

    runner = Runner({"t1-probe": new_file_patch("t1-probe.txt")})
    result, _ = drive(repo, cart, tmp_path, runner=runner, work=work, run_id="epic-one")

    assert any(call["role"] == "build" and "t1-probe" in call["prompt"] for call in runner.calls)
    assert not any(q["id"] == "t1-probe" for q in result["quarantined"])
    assert is_ancestor(repo, "epic/demo-initiative/p1-foundations--t1-probe", "epic/demo-initiative/p1-foundations")


def test_a_refused_build_is_never_applied_and_never_shown_to_a_validator(repo, cart, tmp_path) -> None:
    """The saving, and the correctness, are the same change.

    A patch the reviewers rejected should cost nothing further: no scratch
    branch, no check run, and above all no chunk validator paid to adjudicate a
    `review_verdict` the fix loop already settled.
    """
    runner = RefusedRunner(
        {t: new_file_patch(f"{t}.txt") for t in TASK_IDS}, refused="t1-probe"
    )
    result, runner = drive(repo, cart, tmp_path, runner=runner, work=initiative(two_phases=False))

    # No validator was ever asked about the refused task.
    chunk_prompts = [c["prompt"] for c in runner.calls if c["role"] == "validate_chunk"]
    assert all("t1-probe" not in p for p in chunk_prompts), chunk_prompts
    assert any("t2-bench" in p for p in chunk_prompts), "the healthy sibling is still validated"

    # The patch was never applied: no scratch branch and no draft branch for it.
    assert "agents/epic-1/t1-probe" not in branches(repo)
    assert "epic/demo-initiative/p1-foundations--t1-probe" not in branches(repo)

    # The sibling is unaffected and still merges.
    assert is_ancestor(
        repo, "epic/demo-initiative/p1-foundations--t2-bench", "epic/demo-initiative/p1-foundations"
    )

    # The run's task record says quarantined, with the loop's reason attached.
    refused = next(t for t in result["tasks"] if t["id"] == "t1-probe")
    assert refused["status"] == "quarantined"
    assert refused["evidence"] == [{"check": "fix_loop", "output": refused["quarantine"]}]
    assert "no_progress" in refused["quarantine"]


# ── trim, once validate_phase is satisfied (docs/design/landing-model.md §5) ─

_TRIM_PHASE = "p1-foundations"
_TRIM_BRANCH = "epic/demo-initiative/p1-foundations"
_TEST_FILE_PATCH = new_file_patch("test_thing.py", "def test_a(): assert True")

_DELETE_BENCH_TXT = (
    "diff --git a/t2-bench.txt b/t2-bench.txt\n"
    "deleted file mode 100644\n"
    "--- a/t2-bench.txt\n"
    "+++ /dev/null\n"
    "@@ -1 +0,0 @@\n"
    "-ok\n"
)

_DROP_TEST_ID = (
    "diff --git a/test_thing.py b/test_thing.py\n"
    "deleted file mode 100644\n"
    "--- a/test_thing.py\n"
    "+++ /dev/null\n"
    "@@ -1 +0,0 @@\n"
    "-def test_a(): assert True\n"
)

_BREAK_CHECK = (
    "diff --git a/t2-bench.txt b/t2-bench.txt\n"
    "--- a/t2-bench.txt\n"
    "+++ b/t2-bench.txt\n"
    "@@ -1 +1 @@\n"
    "-ok\n"
    "+not-ok\n"
)


def _with_style(cart: dict) -> dict:
    return {**cart, "skills": {**cart["skills"], "style_pass": "acme-skills:style-pass"}}


def _trim_patches() -> dict[str, str]:
    return {"t1-probe": _TEST_FILE_PATCH, "t2-bench": new_file_patch("t2-bench.txt")}


def test_style_pass_is_invoked_exactly_once_per_phase_completion(repo, cart, tmp_path) -> None:
    runner = Runner(_trim_patches(), style={_TRIM_PHASE: ""})
    result, runner = drive(repo, _with_style(cart), tmp_path, runner=runner, work=initiative(two_phases=False))
    assert result["phases"][0]["status"] == "complete"
    assert sum(1 for c in runner.calls if c["role"] == "style_pass") == 1
    assert "trim" not in result["phases"][0]


def test_a_trim_that_keeps_every_id_and_passes_checks_is_applied_and_counted(repo, cart, tmp_path) -> None:
    runner = Runner(_trim_patches(), style={_TRIM_PHASE: _DELETE_BENCH_TXT})
    result, _ = drive(repo, _with_style(cart), tmp_path, runner=runner, work=initiative(two_phases=False))
    assert result["phases"][0]["status"] == "complete"
    assert result["phases"][0]["trim"] == "trim: 1 lines removed"
    files = git("ls-tree", "-r", "--name-only", _TRIM_BRANCH, cwd=repo).splitlines()
    assert "t2-bench.txt" not in files
    assert "test_thing.py" in files


def test_the_trim_commit_never_stages_a_nested_task_worktree_as_a_gitlink(repo, cart, tmp_path) -> None:
    """`ctx.phase_worktree(phase)` still holds each task's own build worktree when the trim commits."""
    runner = Runner(_trim_patches(), style={_TRIM_PHASE: _DELETE_BENCH_TXT})
    result, _ = drive(repo, _with_style(cart), tmp_path, runner=runner, work=initiative(two_phases=False))
    assert result["phases"][0]["trim"] == "trim: 1 lines removed"
    assert "160000" not in git("ls-tree", "-r", _TRIM_BRANCH, cwd=repo)


def test_a_trim_that_drops_a_collected_id_is_refused_and_the_branch_is_untouched(repo, cart, tmp_path) -> None:
    runner = Runner(_trim_patches(), style={_TRIM_PHASE: _DROP_TEST_ID})
    result, _ = drive(repo, _with_style(cart), tmp_path, runner=runner, work=initiative(two_phases=False))
    assert result["phases"][0]["status"] == "complete"
    assert result["phases"][0]["trim"] == "trim: refused (coverage floor)"
    assert git("show", f"{_TRIM_BRANCH}:test_thing.py", cwd=repo) == "def test_a(): assert True"


def test_a_trim_that_fails_a_configured_check_is_refused_and_the_branch_is_untouched(repo, cart, tmp_path) -> None:
    runner = Runner(_trim_patches(), style={_TRIM_PHASE: _BREAK_CHECK})
    result, _ = drive(repo, _with_style(cart), tmp_path, runner=runner, work=initiative(two_phases=False))
    assert result["phases"][0]["status"] == "complete"
    assert result["phases"][0]["trim"] == "trim: refused (coverage floor)"
    assert git("show", f"{_TRIM_BRANCH}:t2-bench.txt", cwd=repo) == "ok"
