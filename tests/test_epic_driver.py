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

from core import ledger
from graphs._spec import GraphSpec
from graphs.delivery import lifecycle_propose, phase_validate
from harness.epic import phase_order, phase_parents, run_epic
from runner.protocol import RunnerError

SHA = "sha-fixture"
PROFILE = "anthropic-default"

TASK_IDS = ("t1-probe", "t2-bench", "t3-cutover")
PHASE_IDS = ("p1-foundations", "p2-rollout")

APPROVE = {"verdict": "approve", "findings": [], "rationale": "matches the charter"}
CHUNK_OK = {"satisfied": True, "gaps": [], "reasoning": "the description is satisfied"}
CHUNK_BAD = {"satisfied": False, "gaps": ["the probe reads nothing"], "reasoning": "not done"}
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

    def __init__(self, patches: dict[str, str], *, chunk=None, verdicts=None) -> None:
        self.patches = patches
        self.chunk = chunk or {}
        self.verdicts = verdicts or {}
        self.calls: list[dict] = []
        self.lock = threading.Lock()

    def _subject(self, prompt: str, candidates) -> str | None:
        return next((c for c in candidates if c in prompt), None)

    def run(self, *, role, tier, schema, prompt, context=()):
        with self.lock:
            self.calls.append({"role": role, "tier": tier, "prompt": prompt})

        if role == "plan":
            return {"steps": ["do it"], "files_expected": ["x.txt"], "out_of_scope": []}
        if role == "build":
            task = self._subject(prompt, TASK_IDS)
            if task not in self.patches:
                raise RunnerError(f"no build scripted for {task}")
            return {
                "patch": self.patches[task],
                "summary": f"built {task}",
                "files_touched": [f"{task}.txt"],
                "commands_run": [],
            }
        if role == "review_charter":
            return dict(APPROVE)
        if role == "validate_chunk":
            return dict(self.chunk.get(self._subject(prompt, TASK_IDS), CHUNK_OK))
        if role == "validate_phase":
            return dict(self.verdicts.get(self._subject(prompt, PHASE_IDS), GOAL_MET))
        if role == "work_state_arm":
            return {"applied": True, "detail": "state moved"}
        raise RunnerError(f"no scripted response for role '{role}'")


SPECS = {
    "lifecycle": GraphSpec(name="lifecycle", graph_name="lifecycle-propose", run=lifecycle_propose.run),
    "validate": phase_validate.SPEC,
}


def drive(
    repo, cart, tmp_path, *, runner=None, work=None, assume="a", run_id="epic-1", patches=None, fix_attempts=None
):
    runner = runner or Runner(patches if patches is not None else {t: new_file_patch(f"{t}.txt") for t in TASK_IDS})
    result = run_epic(
        initiative=work if work is not None else initiative(),
        repo=repo,
        cartridge=cart,
        runner=runner,
        specs=SPECS,
        run_id=run_id,
        date="2026-09-01",
        max_parallel=3,
        ledger_path=tmp_path / "ledger.jsonl",
        provider_profile=PROFILE,
        runs_dir=tmp_path / "runs",
        worktree_root=cart["landing_areas"]["worktree_root"],
        assume=assume,
        fix_attempts=fix_attempts,
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
    assert "checks failed" in quarantined[0]["reason"]
    assert result["totals"]["tasks_quarantined"] == 1

    # The sibling's work is untouched by its neighbour's failure.
    assert is_ancestor(repo, "epic/demo-initiative/p1-foundations--t2-bench", "epic/demo-initiative/p1-foundations")
    assert "epic/demo-initiative/p1-foundations--t1-probe" not in branches(repo)

    # The validator was told about the quarantine, and its verdict is what
    # decides the phase — which then does not unblock its dependent.
    prompt = next(c["prompt"] for c in runner.calls if c["role"] == "validate_phase")
    assert "t1-probe" in prompt and "checks failed" in prompt
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
    # No merge was even proposed for it: a task the validator says did not do
    # what it said is not a task whose merge should be up for a decision.
    assert not any("t1-probe" in p["target"] for p in result["proposals"] if p["kind"] == "merge_stack")
    assert "epic/demo-initiative/p1-foundations--t1-probe" not in branches(repo)


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


# ── re-entrancy and the stack rebase ────────────────────────────────────────


def test_a_moved_parent_head_proposes_and_executes_a_stack_rebase(repo, cart, tmp_path) -> None:
    work = initiative(two_phases=False)
    first, _ = drive(repo, cart, tmp_path, work=work, run_id="epic-1")
    assert first["phases"][0]["status"] == "complete"
    before = git("rev-parse", "epic/demo-initiative/p1-foundations", cwd=repo)

    # The ground moves under the stack: somebody lands something on main.
    (repo / "moved.md").write_text("moved\n", encoding="utf-8")
    git("add", "-A", cwd=repo)
    git("commit", "-qm", "advance main", cwd=repo)
    assert not is_ancestor(repo, "main", "epic/demo-initiative/p1-foundations")

    done = initiative(two_phases=False, done=("t1-probe", "t2-bench"))
    second, _ = drive(repo, cart, tmp_path, work=done, run_id="epic-2")

    rebases = [p for p in second["proposals"] if p["kind"] == "stack_rebase"]
    assert len(rebases) == 1 and rebases[0]["target"] == "epic/demo-initiative/p1-foundations"
    assert second["totals"]["stacks_rebased"] == 1
    assert is_ancestor(repo, "main", "epic/demo-initiative/p1-foundations")
    assert git("rev-parse", "epic/demo-initiative/p1-foundations", cwd=repo) != before


def test_no_rebase_is_proposed_when_the_stack_is_still_on_its_base(repo, cart, tmp_path) -> None:
    work = initiative(two_phases=False)
    drive(repo, cart, tmp_path, work=work, run_id="epic-1")
    done = initiative(two_phases=False, done=("t1-probe", "t2-bench"))
    second, _ = drive(repo, cart, tmp_path, work=done, run_id="epic-2")
    assert [p for p in second["proposals"] if p["kind"] == "stack_rebase"] == []
    assert second["totals"]["stacks_rebased"] == 0
    assert second["phases"][0]["status"] == "complete"


class Revising(Runner):
    """Reviews everything as `revise`, so the fix loop is the only thing running."""

    def run(self, *, role, tier, schema, prompt, context=()):
        if role == "review_charter":
            with self.lock:
                self.calls.append({"role": role, "tier": tier, "prompt": prompt})
            return {"verdict": "revise", "findings": [], "rationale": "not yet"}
        return super().run(role=role, tier=tier, schema=schema, prompt=prompt, context=context)


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
