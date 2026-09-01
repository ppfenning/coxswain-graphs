"""The harness: the runtime that owns consequences.

Four nouns, one seam each:

    harness    side effects, policy, the gate, the ledger — THIS package
    graph      a program the harness runs; owns sequence, writes nothing
    cartridge  domain configuration: role -> skill, where writes land
    runner     execution backend: scripted in tests, the Messages API live

A graph is `run(args, runner) -> dict` and registers itself by declaring a
`SPEC`; the harness discovers it, obtains its inputs, runs it, splits its
proposals by the autonomy policy, gates what has not earned autonomy, applies
what was approved through the arm the cartridge names, and records the run.
Graphs stay pure because this package is not. That is the trade, and it is
deliberate: everything worth unit-testing lives on the other side of it.
"""

from harness.autonomy import split_by_policy
from harness.checks import all_passed, checks_evidence, run_checks
from harness.cos import assemble_docket, run_cos
from harness.epic import run_epic
from harness.escalate import escalate_self_modification, governance_hits, touched_paths
from harness.gate import APPLY_SCHEMA, apply_arm_for, apply_decisions, auto_apply, gate
from harness.invoke import Invocation, InvokeError, invoke_graphs
from harness.phase import run_phase
from harness.registry import DiscoveryError, GraphSpec, Need, discover
from harness.resolve import resolve_cartridge, role_skill_bodies
from harness.runners import build_runner
from harness.worktree import apply_patch, create_worktree

__all__ = [
    "APPLY_SCHEMA",
    "DiscoveryError",
    "GraphSpec",
    "Invocation",
    "InvokeError",
    "Need",
    "all_passed",
    "apply_arm_for",
    "apply_decisions",
    "apply_patch",
    "assemble_docket",
    "auto_apply",
    "build_runner",
    "checks_evidence",
    "create_worktree",
    "discover",
    "escalate_self_modification",
    "gate",
    "governance_hits",
    "invoke_graphs",
    "resolve_cartridge",
    "role_skill_bodies",
    "run_checks",
    "run_cos",
    "run_epic",
    "run_phase",
    "split_by_policy",
    "touched_paths",
]

# Compatibility aliases for the shell-era private names, so downstream code and
# tests written against `shell._split_by_policy` migrate by changing an import
# rather than a vocabulary. New code uses the public names above.
_split_by_policy = split_by_policy
_auto_apply = auto_apply
_apply_arm_for = apply_arm_for
_gate = gate
_apply_decisions = apply_decisions
_apply_patch = apply_patch
_run_phase = run_phase
