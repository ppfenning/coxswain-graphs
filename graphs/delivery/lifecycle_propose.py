"""lifecycle-propose — the development loop for ONE task.

    scope -> plan -> [plan_alternative -> plan_arbitrate] -> [plan_adversary]
          -> build (worktree) -> handoff -> review -> adversary -> arbitrate -> emit

Takes one task, produces reviewed work and proposals. Nothing is pushed, opened,
or merged. The build node returns a patch; applying it is the shell's job,
inside a worktree the shell owns.

**Solutions compete before anyone builds.** The reviewers after `build` judge
one diff; they can say ship, revise or reject, and nothing else. A different
design only ever appears if the one planner happens to think of it. So the
bracketed nodes let a team hold a competition at the cheap end of the loop: a
second planner writes an independent plan told to differ, an arbiter picks
one or merges them and names the price, and an adversary attacks the winning
plan's claims — files it assumes, steps that cannot be checked, scope that
grew — before a builder spends a budget on it. A plan costs a tenth of a
build, which is why the competition happens here and not between two diffs.
An objection the adversary sustains buys one revision, and the revision goes
back to whoever wrote the plan — the first planner on its thread, the second
on its own, or the arbiter when the plan is a merge of both. A planner is
never handed another planner's plan and told it is its own. Every one of
these roles is optional; unbound means absent. Even bound, the competition and
the attack run only when the tier read off the task's surfaces and patterns
reaches `policy.plan_competition.min_tier` — a docs-only task does not buy a
second planner just because a team happens to have one bound.

Three convictions shape the back half of this graph.

**Nothing is one-shot.** Every change gets a reviewer, and how many it gets is
proportional to what a mistake would cost — `review_tier` decides, not the
author. A dangerous surface earns an adversary and an arbitrator even when the
diff is four lines.

**A step never builds on an unvalidated handoff.** The `handoff` node checks
that what build produced actually satisfies what review needs before review sees
it, and REFUSES rather than passing a gap along. A phase that goes quietly wrong
usually did so three steps earlier.

**A fix loop must never launder struggle into trust.** Sending a rejected change
back to the builder is ordinary; forgetting that it was sent back is not. The
loop counts its attempts and carries the count out on the proposal, so a task
that passed on the third try stays distinguishable, everywhere downstream, from
one that passed clean. The ledger is what refuses to let a repeated-attempt pass
extend a streak — but it can only refuse what it can see, and this graph is the
only place that knows. A graph that quietly retried until something passed would
be manufacturing exactly the clean record the ledger exists to disbelieve.

Every node after `build` is an optional role: a team that binds none of them
gets the original single-reviewer loop, which is what optional means.

**Elevation.** A ticket's `tier:` map, `{role: tier}`, arrives as `args["tier"]`
and reaches every call this graph makes. A caller-named tier beats the profile
default in the runner's resolver. Where a call site still passes a literal tier,
the ticket's value wins only when it is higher: a ticket never lowers a literal.
The one automatic escalation is the second build after a review `revise`, which
asks one tier up and leaves a `tier escalation` evidence row. A ticket `tier:`
for `build` overrides it. A third attempt is never escalated: third attempts
landed nothing on 09-06.

Next step, not in this repository: per-launch elevation,
`cox route launch epic --elevate ROLE=TIER` (repeatable), lives in coxswain-tools.

Deferred (see graphs/lifecycle-propose.md): intake queue, verification, retro.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from difflib import SequenceMatcher
from typing import Any, Literal, NamedTuple

from graphs._contract import (
    ContractViolation,
    epic_shape,
    landing_for,
    proposal,
    require,
    require_cartridge,
    review_tier,
)
from graphs.delivery.phase_validate import _PLACEHOLDER_MARKERS
from runner.decision_log import RouterDecision
from runner.decision_source import DecisionSource, ask
from runner.protocol import BudgetStop, NodeResult, NodeRunner, RunnerError
from runner.system_one import Answer
from runner.tier_resolution import TIERS, Hints, rank

__all__ = ["GRAPH_NAME", "review_is_placeholder", "run"]

GRAPH_NAME = "lifecycle-propose"

SCOPE_SCHEMA = {
    "type": "object",
    "properties": {
        "phases": {"type": "array", "items": {"type": "string"}},
        "tickets": {"type": "array", "items": {"type": "string"}},
        "repos": {"type": "array", "items": {"type": "string"}},
        "state": {"type": "string", "enum": ["active", "planned", "future"]},
        "parent_epic": {"type": "string", "description": "existing epic to attach to, or empty"},
        "rationale": {"type": "string"},
    },
    "required": ["phases", "tickets", "repos", "state", "parent_epic", "rationale"],
    "additionalProperties": False,
}

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "steps": {"type": "array", "items": {"type": "string"}},
        "files_expected": {"type": "array", "items": {"type": "string"}},
        "out_of_scope": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["steps", "files_expected", "out_of_scope"],
    "additionalProperties": False,
}

BUILD_SCHEMA = {
    "type": "object",
    "properties": {
        "patch": {"type": "string", "description": "unified diff, applied by the shell in its own worktree"},
        "summary": {"type": "string"},
        "files_touched": {"type": "array", "items": {"type": "string"}},
        "commands_run": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"command": {"type": "string"}, "output": {"type": "string"}},
                "required": ["command", "output"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["patch", "summary", "files_touched", "commands_run"],
    "additionalProperties": False,
}

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "revise", "reject"]},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "charter_principle": {"type": "string"},
                    "detail": {"type": "string"},
                    "file": {"type": "string"},
                },
                "required": ["charter_principle", "detail", "file"],
                "additionalProperties": False,
            },
        },
        "rationale": {"type": "string"},
    },
    "required": ["verdict", "findings", "rationale"],
    "additionalProperties": False,
}


HANDOFF_SCHEMA = {
    "type": "object",
    "properties": {
        "complete": {"type": "boolean"},
        # Not every refusal costs the same thing, so the shuttle has to say which
        # kind it is. Missing ARTIFACT or missing INPUT — no patch, a file the
        # plan needed that nobody produced — is blocking: there is nothing for a
        # builder to improve and the line stops. Missing EVIDENCE or a thin
        # summary, with the artifact present, is not: the work exists and what
        # it lacks is exactly what one more build attempt can add.
        "blocking": {
            "type": "boolean",
            "description": (
                "true when the artifact itself or an input the plan needed is missing; "
                "false when the artifact is present and only its evidence or summary is incomplete"
            ),
        },
        "missing": {"type": "array", "items": {"type": "string"}},
        "brief": {"type": "string", "description": "the small thing the next step actually needs"},
    },
    "required": ["complete", "blocking", "missing", "brief"],
    "additionalProperties": False,
}

ADVERSARY_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "revise", "reject"]},
        "objections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"claim": {"type": "string"}, "why_wrong": {"type": "string"}},
                "required": ["claim", "why_wrong"],
                "additionalProperties": False,
            },
        },
        "strongest_objection": {"type": "string"},
    },
    "required": ["verdict", "objections", "strongest_objection"],
    "additionalProperties": False,
}

ARBITRATE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "revise", "reject"]},
        "sided_with": {"type": "string", "enum": ["charter", "adversary", "neither"]},
        "reasoning": {"type": "string"},
    },
    "required": ["verdict", "sided_with", "reasoning"],
    "additionalProperties": False,
}

PLAN_CHOICE_SCHEMA = {
    "type": "object",
    "properties": {
        "chosen": {"type": "string", "enum": ["first", "second", "merged"]},
        # Not required: a pick hands the source plan over verbatim, so a plan
        # emitted alongside `first` or `second` is a plan nobody reads, and a
        # deep-tier model should not have to write one to be discarded. A
        # merge with no plan is enforced in _plan_competition, not here: this
        # schema goes to the model's structured-output subset, which has no
        # if/then, and a merge that names no plan is a stopped graph, not a
        # fallback to either planner's.
        "plan": {**PLAN_SCHEMA, "description": "the merged plan; required when chosen is 'merged', omitted on a pick"},
        "reasoning": {"type": "string"},
        "price": {"type": "string", "description": "what choosing this plan costs, in one sentence"},
    },
    "required": ["chosen", "reasoning", "price"],
    "additionalProperties": False,
}

PLAN_ATTACK_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["proceed", "revise"]},
        "objections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"claim": {"type": "string"}, "why_wrong": {"type": "string"}},
                "required": ["claim", "why_wrong"],
                "additionalProperties": False,
            },
        },
        "strongest_objection": {"type": "string"},
    },
    "required": ["verdict", "objections", "strongest_objection"],
    "additionalProperties": False,
}


DEFAULT_FIX_ATTEMPTS = 2


class _Author(NamedTuple):
    """Who wrote a plan: the role, its tier, and the thread it was written on.

    A revision has to go back to the seat that holds the plan's context. The
    first planner keeps the ticket's thread, which build later joins; the
    second planner and the arbiter each keep a thread of their own, so that a
    revision can continue where they left off without ever joining the first
    planner's — independence is the whole value of a second plan.
    """

    role: str
    tier: str
    thread: str


def _ticket_text(ticket: Any, title: Any, body: Any) -> str:
    """Pure: the id, then the title and body when the harness supplied them."""
    parts = [str(ticket)]
    if title:
        parts[0] = f"{ticket} — {title}"
    if body:
        parts.append(str(body).strip())
    return "\n".join(parts)

# How much of a patch the handoff sees: all of it, up to a bound that only a
# pathological diff reaches. A 6,000-character preview was tried first and the
# handoff — correctly — refused every patch it could see was cut off. The
# shuttle judges the cargo; it cannot judge half of it.
PATCH_PREVIEW_CHARS = 200_000

# Two successive patches this similar are the same patch with the whitespace
# moved. 0.98 rather than 1.0 because a builder that re-emits its own diff
# rarely re-emits it byte-identically, and "it changed a comment" is not the
# objection falling.
NO_PROGRESS_RATIO = 0.98

# A budget stop is not a failure to discard; the worktree holds the partial
# patch and the session holds the context. Two continuations, not unbounded —
# past this the task is too large for the slice it was given, and that is a
# scoping problem, not a reason to keep burning budget on the same session.
CONTINUATIONS_MAX = 2


def is_test_path(path: str) -> bool:
    """A file whose changes are evidence for a change, not the change itself."""
    name = path.rsplit("/", 1)[-1]
    return (
        path.startswith("tests/")
        or (name.startswith("test_") and name.endswith(".py"))
        or name.endswith("_test.py")
        or name == "conftest.py"
    )


_DIFF_PREFIXES = ("a/", "b/", "c/", "i/", "w/", "o/")
_DIFF_GIT_RE = re.compile(r"^diff --git \S+ (\S+)$", re.MULTILINE)


def _diff_path(token: str) -> str:
    return token[2:] if token[:2] in _DIFF_PREFIXES else token


def _file_chunks(patch: str) -> list[tuple[str, str]]:
    """One `(path, chunk)` per `diff --git` header, path taken from its `b/` (or `i/`) side."""
    headers = list(_DIFF_GIT_RE.finditer(patch))
    ends = [h.start() for h in headers[1:]] + [len(patch)]
    return [(_diff_path(header.group(1)), patch[header.end():end]) for header, end in zip(headers, ends)]


def _changed_in(chunk: str) -> int:
    return sum(
        1
        for line in chunk.splitlines()
        if (line.startswith("+") and not line.startswith("+++")) or (line.startswith("-") and not line.startswith("---"))
    )


def _lines_by_kind(patch: str) -> tuple[int, int]:
    """Added-plus-removed lines per file in the patch, split source from test."""
    chunks = _file_chunks(patch) or [("", patch)]
    source_lines = sum(_changed_in(chunk) for path, chunk in chunks if not is_test_path(path))
    test_lines = sum(_changed_in(chunk) for path, chunk in chunks if is_test_path(path))
    return source_lines, test_lines


_HUNK_RE = re.compile(r"^@@ -\d+,(\d+) \+\d+,(\d+) @@")


def patch_parses(patch: str) -> str | None:
    """None when every hunk header's counts match its body and the text ends with a newline."""
    if not patch.strip():
        return None
    name, hunk_index = "the patch", 0
    for path, chunk in _file_chunks(patch) or [("", patch)]:
        name = path or "the patch"
        lines = chunk.splitlines()
        hunk_index = 0
        for i, line in enumerate(lines):
            match = _HUNK_RE.match(line)
            if not match:
                continue
            hunk_index += 1
            old_count, new_count = int(match.group(1)), int(match.group(2))
            body: list[str] = []
            for later in lines[i + 1 :]:
                if later.startswith("@@"):
                    break
                body.append(later)
            seen_old = sum(1 for l in body if not l.startswith("+"))
            seen_new = sum(1 for l in body if not l.startswith("-"))
            if seen_old != old_count or seen_new != new_count:
                return f"the patch was cut off at {name} hunk {hunk_index}; emit the complete patch"
    if not patch.endswith("\n"):
        return f"the patch was cut off at {name} hunk {hunk_index}; emit the complete patch"
    return None


def _hints(
    patch: str,
    *,
    attempt: int | None = None,
    judgment: Literal["low", "normal", "high"] | None = None,
) -> Hints:
    """Call hints: files and added-plus-removed lines counted from `patch`, not asked of a model."""
    chunks = _file_chunks(patch)
    return Hints(
        judgment=judgment,
        files_changed=len(chunks) or None,
        lines_changed=sum(_changed_in(chunk) for _, chunk in chunks or [("", patch)]) if patch.strip() else None,
        attempt=attempt,
    )


def _change_facts(build: Mapping[str, Any]) -> dict[str, Any]:
    """Deterministic facts about the change, for the reviewer and the gate.

    Counted from the patch rather than asked of the model: a node reporting its
    own diff size is reporting a recollection, and the review tier keys off
    these numbers.
    """
    patch = build.get("patch") or ""
    lines = patch.splitlines()
    added = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
    files = list(build.get("files_touched") or [])
    source_lines, test_lines = _lines_by_kind(patch)
    return {
        "files_touched": files,
        "module_count": len({f.rsplit("/", 1)[0] for f in files}),
        "added_lines": added,
        "removed_lines": removed,
        "changed_lines": added + removed,
        "source_lines": source_lines,
        "test_lines": test_lines,
        "patch_ok": patch_parses(patch) is None,
    }


def harness_verify_block(build: Mapping[str, Any]) -> str:
    """The `harness_verify` rows, quoted verbatim for a reviewer prompt; "" when there are none."""
    rows = [
        entry
        for entry in build.get("commands_run") or []
        if isinstance(entry, Mapping) and entry.get("source") == "harness_verify"
    ]
    if not rows:
        return ""
    quoted = "\n".join(f"$ {row.get('command')}\n{row.get('output')}" for row in rows)
    return (
        "Harness-run verify evidence: the harness ran these commands in the build's own scratch "
        "after the patch. They are not builder claims. Quoted verbatim:\n" + quoted + "\n"
    )


def measured_facts(build: Mapping[str, Any], change_facts: Mapping[str, Any]) -> dict[str, str]:
    """The facts the harness itself established, keyed for `prune_missing` to cite.

    Every path the build touched, the five line counts, and the tail of every
    `pytest` command it ran — all of it already measured, none of it asked of a
    model. A `missing` item that names one of these is asking for a number the
    graph is already holding.
    """
    facts: dict[str, str] = {}
    for path in change_facts.get("files_touched") or []:
        facts[f"file:{path}"] = f"touched: {path}"
    for key in ("added_lines", "removed_lines", "changed_lines", "source_lines", "test_lines"):
        facts[key] = f"{key.replace('_', ' ')}: {change_facts.get(key)}"
    for index, entry in enumerate(build.get("commands_run") or []):
        command = str(entry.get("command") or "") if isinstance(entry, Mapping) else ""
        if not command.startswith("pytest"):
            continue
        output = str(entry.get("output") or "") if isinstance(entry, Mapping) else ""
        tail = "\n".join(output.strip().splitlines()[-3:])
        facts[f"pytest:{index}"] = f"{command} -> {tail}"
    return facts


# The model's own words for "I already have this"; matched case-insensitively
# against a `missing` item, then against the fact-key substring that answers
# it. A complaint typed differently still names the same absent thing.
_DISCHARGE_PHRASES = {
    "files touched": "file",
    "git diff": "file",
    "diff shown": "file",
    "line count": "lines",
    "lines": "lines",
    "size": "lines",
    "budget": "lines",
    "pytest": "pytest",
    "test output": "pytest",
    "suite": "pytest",
    "passed": "pytest",
}


def prune_missing(missing: list[str], facts: dict[str, str]) -> tuple[list[str], list[str]]:
    """Split a model's `missing` list into what still stands and what the
    harness already answered.

    A complaint that names a fact the harness already measured is discharged,
    paired with the fact that answers it — a reader sees the harness answered
    it rather than a build attempt spent closing a gap that was never open.
    """
    kept: list[str] = []
    discharged: list[str] = []
    for item in missing:
        # Word-bounded: a phrase must appear as itself, not as a run of
        # letters inside a longer word — "cleanliness" is not "lines".
        substring = next(
            (sub for phrase, sub in _DISCHARGE_PHRASES.items() if re.search(rf"\b{re.escape(phrase)}\b", item, re.IGNORECASE)),
            None,
        )
        fact = next((value for key, value in facts.items() if substring and substring in key), None)
        if fact is not None:
            discharged.append(f"{item} -> {fact}")
        else:
            kept.append(item)
    return kept, discharged


_COMMAND_WORDS = frozenset({"uv", "python", "python3", "grep", "rg", "make", "npm", "bash", "sh", "cat", "sed", "cox", "git"})
_BACKTICK_SPAN_RE = re.compile(r"`([^`]+)`")


def impossible_evidence(item: str, prefixes: Sequence[str]) -> bool:
    """True when a backticked command in `item` starts with none of the build session's permitted `prefixes`."""
    spans = (span.strip() for span in _BACKTICK_SPAN_RE.findall(item))
    return any(
        span.split()[0] in _COMMAND_WORDS and not any(span.startswith(prefix) for prefix in prefixes)
        for span in spans
        if span
    )


_SIZE_TARGET_RE = re.compile(r"~\s*(\d+)\s*lines", re.IGNORECASE)


def _size_deviation(ticket_text: str, source_lines: int, test_lines: int) -> str | None:
    """Overrun of `source_lines` against the ticket's `~N lines` target; test lines are reported, not counted."""
    match = _SIZE_TARGET_RE.search(ticket_text)
    if not match:
        return None
    target = int(match.group(1))
    if source_lines <= target:
        return None
    return f"deviation: {source_lines} source lines against ~{target} (tests {test_lines} lines, not counted)"


def _test_ratio_deviation(source_lines: int, test_lines: int) -> str | None:
    """A disclosed deviation, never a refusal, when tests dwarf the source they cover."""
    if source_lines <= 0 or test_lines <= 3 * source_lines:
        return None
    return f"deviation: tests are {test_lines / source_lines:.1f}x the source"


def _claims(adversary: Mapping[str, Any] | None) -> set[str]:
    """The adversary's objections, normalised for comparison across rounds.

    Case and surrounding whitespace are not the objection. The same complaint
    typed differently in the next round is still the same complaint, still
    standing — and a comparison strict enough to miss that would let a loop
    re-litigate one objection until the cap ran out.
    """
    if not adversary:
        return set()
    return {
        str(objection.get("claim") or "").strip().lower()
        for objection in adversary.get("objections") or []
        if isinstance(objection, Mapping) and str(objection.get("claim") or "").strip()
    }


def round_summary(
    attempt: int,
    source: str,
    review: Mapping[str, Any],
    adversary: Mapping[str, Any] | None,
    arbitration: Mapping[str, Any] | str | None,
    verdict: str,
) -> dict[str, Any]:
    """One handoff or review round, reduced to who spoke and how it ended.

    A skipped-arbiter marker is a string, not a decision, and reads as None.
    """
    return {
        "attempt": attempt,
        "source": source,
        "verdict": verdict,
        "findings": [str(f.get("charter_principle")) for f in review.get("findings") or [] if isinstance(f, Mapping)],
        "objections": len(adversary.get("objections") or []) if adversary else 0,
        "arbitration": (
            arbitration.get("decision", arbitration.get("verdict")) if isinstance(arbitration, Mapping) else None
        ),
    }


def _arbiter_scope(verdict: str, arbitration: Mapping[str, Any] | None) -> set[str] | None:
    """Backticked, path-shaped tokens in a revise arbitration's `reasoning`
    (a `/` or a file suffix); None outside that case. Backticked identifiers
    such as a renamed key are house style, not scope — a reasoning that
    names none reads as an empty scope, which means any file."""
    if verdict != "revise" or not arbitration:
        return None
    tokens = re.findall(r"`([^`\s]+)`", str(arbitration.get("reasoning") or ""))
    return {t for t in tokens if _PATH_TOKEN.match(t) and ("/" in t or re.search(r"\.[A-Za-z0-9]{1,5}$", t))}


# A scope entry is a repository path, nothing else: `glob("*/*.json")` and
# `f.stem` carry a slash or a suffix but are code, and admitting them once
# scoped a real revision to files nobody could touch — which is no_progress.
_PATH_TOKEN = re.compile(r"^[\w.\-]+(?:/[\w.\-]+)*$")


def _patch_sections(patch: str) -> dict[str, str]:
    """Each touched file's own diff text, sliced between its `+++ b/<path>` headers."""
    # Builders emit `a/`+`b/` or, under diff.mnemonicPrefix, `c/`+`i/`/`w/`;
    # any one-letter prefix is stripped so the key is the repository path.
    marks = list(re.finditer(r"^\+\+\+ (?:[a-z]/)?(\S+)", patch, re.MULTILINE))
    return {m.group(1): patch[m.end() : n.start() if n else len(patch)] for m, n in zip(marks, marks[1:] + [None])}


# A ticket's own contract commands, always backtick-fenced in its prose.
_CONTRACT_COMMAND_RE = re.compile(r"`(pytest -q[^`\n]*|ruff check \.)`")


def _build_output_valid(build: Mapping[str, Any], ticket_text: str) -> str | None:
    """None when the patch's files match `files_touched` and its contract commands ran with output."""
    files, touched = set(_patch_sections(build.get("patch") or "")), set(build.get("files_touched") or [])
    if files != touched:
        return f"files_touched {sorted(touched)} does not match the patch's files {sorted(files)}"
    run = [(str(e.get("command") or "").strip(), str(e.get("output") or "").strip())
           for e in build.get("commands_run") or [] if isinstance(e, Mapping)]
    missing = [c for c in _CONTRACT_COMMAND_RE.findall(ticket_text)
               if not any(_contract_command_matches(c, cmd) and out for cmd, out in run)]
    return f"commands_run has no output for {missing}" if missing else None


def _contract_command_matches(contract: str, ran: str) -> bool:
    """A contract line names a shape, not a byte string: `<your test file>`
    placeholders stand for one token, and the builder may chain or wrap the
    command (`… && ruff check .`, `… 2>&1 | tail -20`), so the contract must
    appear inside what ran, whitespace-normalised."""
    # A quoted command that trails off — `pytest -q <file> ...` in a chair
    # note — names its head, not a byte string: the tail is open.
    contract = re.sub(r"\s*(\.\.\.|\u2026)\s*$", "", " ".join(contract.split()))
    ran = " ".join(ran.split())
    parts = [re.escape(p) for p in re.split(r"<[^>]*>", contract)]
    if re.search(r"\S+".join(parts), ran) is not None:
        return True
    # A pytest line names files to prove, not an argv to reproduce: a run
    # that covers every named file — or a bare `pytest -q` over the whole
    # suite — is the same evidence, however the builder spelled it.
    want, got = _pytest_files(contract), _pytest_files(ran)
    if want is None or got is None:
        return False
    return not got or want <= got


def _pytest_files(command: str) -> set[str] | None:
    """The test paths a `pytest` invocation names (empty set = whole suite); None if it is not one."""
    head = command.split("|")[0].split("&&")[0].split(";")[0]
    if not re.match(r"^\s*(python\S*\s+-m\s+)?pytest\b", head):
        return None
    return {t for t in head.split()[1:] if not t.startswith("-") and (t.endswith(".py") or "/" in t)}


def _is_budget_stop(exc: Exception) -> bool:
    """Whether a `RunnerError` is the CLI's dollar-ceiling stop, not some other failure."""
    return "error_max_budget_usd" in str(exc).lower()


def _continue_ok(stop: BudgetStop, *, surfaces: list[str], continuations: int) -> tuple[bool, str]:
    """Whether a budget-stopped build is worth resuming, and why not when it isn't.

    A fact-only check — no model is asked whether to continue, so it costs
    nothing to run on every stop. The three no-go reasons are different
    signals and each names its own remedy: whoever reads the quarantine acts
    on the text, not on a bare "no".
    """
    if not stop.session:
        return False, "no session to resume"

    touched = [
        _diff_path(line[len("+++ ") :].strip())
        for line in (stop.partial_patch or "").splitlines()
        if line.startswith("+++ ")
    ]
    if surfaces:
        # A surface written `path (new)` names the path; the marker is the
        # decompose's note that the file does not exist yet, not part of it.
        declared = {re.sub(r"\s*\([^)]*\)\s*$", "", str(s_)) for s_ in surfaces}
        outside = [path for path in touched if path not in declared]
        if outside:
            return False, (
                f"partial work touches {outside[0]} outside the task's surfaces: "
                "re-scope the task"
            )

    if not (stop.partial_patch or "").strip():
        if continuations == 0:
            return True, ""
        return False, (
            "two budget slices produced no change: set budget_usd on the work "
            "item if the task is legitimately this large, otherwise the task "
            "body is the problem"
        )

    if continuations < CONTINUATIONS_MAX:
        return True, ""

    untouched = [surface for surface in surfaces if surface not in touched]
    return False, (
        "continuation cap reached with partial work: split recommended — "
        f"done: {', '.join(touched)}; untouched: {', '.join(untouched)}"
    )


def _tier_for(role: str, literal: str | None, ticket_tiers: Mapping[str, str]) -> str | None:
    """The tier a call asks for: the ticket's, only when it is higher than the literal.

    A ticket never lowers a literal. With no literal the ticket's tier is
    named outright, which beats the profile default in the runner's resolver.
    An unknown tier name raises ValueError from `rank`.
    """
    mapped = ticket_tiers.get(role)
    if mapped is None:
        return literal
    if literal is None:
        return mapped
    return mapped if rank(mapped) > rank(literal) else literal


def _escalate(tier: str) -> str:
    """One tier up; the top tier stays where it is."""
    return TIERS[min(rank(tier) + 1, len(TIERS) - 1)]


class _Elevated:
    """A runner whose every call carries the ticket's per-role tier, via `_tier_for`.

    `run` takes exactly the protocol's parameters and no `**kwargs`: a runner
    accepts nothing else, so anything extra would be a TypeError on a live call.
    """

    def __init__(self, inner: NodeRunner, ticket_tiers: Mapping[str, str]) -> None:
        self._inner = inner
        self._ticket_tiers = dict(ticket_tiers)

    def run(
        self,
        *,
        role: str,
        tier: str | None = None,
        hints: Hints | None = None,
        schema: Mapping[str, Any],
        prompt: str,
        context: Sequence[str] = (),
        thread: str | None = None,
        budget_usd: float | None = None,
        task: str | None = None,
    ) -> NodeResult:
        return self._inner.run(
            role=role,
            tier=_tier_for(role, tier, self._ticket_tiers),
            hints=hints,
            schema=schema,
            prompt=prompt,
            context=context,
            thread=thread,
            budget_usd=budget_usd,
            task=task,
        )


class _Asking:
    """A runner whose every call carries the source's decision for its role and hints.

    The decision is a shadow value: `tier` and every other argument pass through
    untouched. A None decision is left off the call, so a runner written before
    `router_decision` existed still accepts it.
    """

    def __init__(self, inner: NodeRunner, source: DecisionSource) -> None:
        self._inner = inner
        self._source = source

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def run(
        self,
        *,
        role: str,
        tier: str | None = None,
        hints: Hints | None = None,
        schema: Mapping[str, Any],
        prompt: str,
        context: Sequence[str] = (),
        thread: str | None = None,
        budget_usd: float | None = None,
        task: str | None = None,
        router_decision: RouterDecision | None = None,
    ) -> NodeResult:
        decision = ask(self._source, role, hints)
        extra = {"router_decision": decision} if decision is not None else {}
        return self._inner.run(
            role=role,
            tier=tier,
            hints=hints,
            schema=schema,
            prompt=prompt,
            context=context,
            thread=thread,
            budget_usd=budget_usd,
            task=task,
            **extra,
        )


def _resume_build(
    runner: NodeRunner,
    *,
    context: list[str],
    ticket: Any,
    budget_usd: float | None,
    surfaces: list[str],
    stop: BudgetStop,
    continuations: int,
    tier: str = "standard",
) -> tuple[dict[str, Any] | None, int, str, BudgetStop]:
    """Decide go/no-go on a budget stop and, on go, resume until one finishes
    or the cap refuses another.

    Returns the finished build (`None` on no-go), the updated continuation
    count, the no-go reason (empty on go), and the last `BudgetStop` seen —
    the caller needs it to report what a first-build no-go could not keep.
    """
    while True:
        go, reason = _continue_ok(stop, surfaces=surfaces, continuations=continuations)
        if not go:
            return None, continuations, reason, stop
        try:
            build = runner.run(
                role="build",
                tier=tier,
                thread=str(ticket),
                task=str(ticket),
                schema=BUILD_SCHEMA,
                context=context,
                budget_usd=budget_usd,
                prompt=(
                    "Your previous session stopped at its budget ceiling. Nothing "
                    "you did is lost: the worktree holds your partial change and "
                    "this session holds your context. Continue from exactly where "
                    "you stopped — do not start over and do not re-read what you "
                    "already read. Finish the change, run the checks, and return "
                    "the unified diff of the WHOLE change."
                ),
            )
        except BudgetStop as exc:
            continuations += 1
            stop = exc
            continue
        continuations += 1
        return dict(build), continuations, "", stop


def _critique(
    review: Mapping[str, Any],
    adversary: Mapping[str, Any] | None,
    arbitration: Mapping[str, Any] | None,
) -> str:
    """Everything the reviewers held against the change, as one block of text.

    The whole critique, not a summary of it. A builder handed "review asked for
    changes" will fix the thing it already thought was wrong; a builder handed
    the objection verbatim has to answer that objection.
    """
    lines = [f"Charter reviewer: {review.get('verdict')} — {review.get('rationale')}"]
    lines += [
        f"- finding ({finding.get('charter_principle')}) in {finding.get('file')}: {finding.get('detail')}"
        for finding in review.get("findings") or []
        if isinstance(finding, Mapping)
    ]
    if adversary is not None:
        lines.append(f"Adversary: {adversary.get('verdict')} — strongest: {adversary.get('strongest_objection')}")
        lines += [
            f"- objection: {objection.get('claim')} — {objection.get('why_wrong')}"
            for objection in adversary.get("objections") or []
            if isinstance(objection, Mapping)
        ]
    if isinstance(arbitration, Mapping):
        lines.append(f"Arbitration sided with {arbitration.get('sided_with')}: {arbitration.get('reasoning')}")
    return "\n".join(lines)


def _plan_competition(
    runner: NodeRunner,
    *,
    context: list[str],
    ticket: Any,
    date: Any,
    plan: Mapping[str, Any],
    first: _Author,
    task_id: Any,
) -> tuple[dict[str, Any], dict[str, Any], _Author]:
    """A second, independent plan, and a decision between the two.

    The second planner is shown the first plan only so it can avoid repeating
    it — it is told to differ, not to critique — and it never joins the first
    planner's thread, for the same reason review never joins the builder's.
    The arbiter picks, or merges. When it picks, the builder gets the source
    plan VERBATIM, not the arbiter's restatement of it: what was compared is
    what gets built. Only a merge is the arbiter's own plan.

    Returns the winning plan, the record, and the winner's AUTHOR — the seat
    a revision goes back to. A pick's author is the planner that wrote it; a
    merge's author is the arbiter, because neither planner wrote that plan.
    """
    second = _Author("plan_alternative", "standard", f"{first.thread}/plan_alternative")
    arbiter = _Author("plan_arbitrate", "deep", f"{first.thread}/plan_arbitrate")
    alternative = dict(
        runner.run(
            role=second.role,
            tier=second.tier,
            thread=second.thread,
            task=str(task_id),
            schema=PLAN_SCHEMA,
            context=context,
            prompt=(
                "A first plan for this ticket already exists. Write a second one "
                "that takes a materially different route — a different "
                "decomposition, different files, or a different order — so that "
                "a comparison is worth making. Do not critique the first plan; "
                "produce a whole plan of your own.\n\n"
                f"Ticket: {ticket}\nDate: {date}\n\n"
                f"First plan (do not repeat it): {plan}\n\n"
                "Name the files you expect to touch, and state what is explicitly "
                "out of scope."
            ),
        )
    )
    choice = dict(
        runner.run(
            role=arbiter.role,
            tier=arbiter.tier,
            thread=arbiter.thread,
            task=str(task_id),
            schema=PLAN_CHOICE_SCHEMA,
            context=context,
            prompt=(
                "Two independent plans exist for this ticket. Choose the one the "
                "builder should carry out, or merge them into one that is better "
                "than either. Judge them against the ticket and the repository in "
                "front of you: steps that can be checked, files that exist, scope "
                "that stays bounded. Say what choosing it costs.\n\n"
                f"Ticket: {ticket}\n\nFirst plan: {plan}\n\nSecond plan: {alternative}"
            ),
        )
    )
    chosen = str(choice.get("chosen"))
    merged = choice.get("plan")
    if chosen == "merged" and not merged:
        raise ContractViolation(
            f"plan_arbitrate claimed 'merged' for '{first.thread}' but returned no plan. "
            "A merge is the arbiter's own plan; the graph stops rather than "
            "building the first plan under that name — a revision would go "
            "back to the arbiter carrying a plan it never wrote."
        )
    winner, author = (
        (dict(plan), first) if chosen == "first"
        else (alternative, second) if chosen == "second"
        else (dict(merged), arbiter)
    )
    record = {
        "alternative": alternative,
        "chosen": chosen,
        "reasoning": str(choice.get("reasoning") or ""),
        "price": str(choice.get("price") or ""),
    }
    return winner, record, author


def _plan_tier(cartridge: Mapping[str, Any], *, surfaces: list[str], patterns: list[str]) -> int:
    """The tier a task reads as before anyone has built anything.

    Deliberately not a call into `review_tier`: a plan has no diff yet, so
    change_facts would have to be passed in empty, and `review_tier`'s size
    branches read an empty dict as zero changed lines in zero modules — which
    satisfies `tier0_max_changed_lines` and `tier1_max_changed_lines` for
    every task. A cartridge that sets `tier0_max_changed_lines` (one already
    does, for live epics) would then read every task without a tier2 surface
    as tier 0 before a line of code exists, silently disabling the
    competition under any floor above 0. This reads only the two policy keys
    a pre-build task can actually speak to — the dangerous surfaces and the
    trivial patterns — and never reaches a size branch at all.

    Note for the next reader: this graph never imports `core`, the same way
    it never imports `shell` or `harness` — only the harness imports the
    substrate, which is what lets CI collect this file with agent-cartridges
    absent.
    """
    config = (cartridge.get("policy") or {}).get("review_tier") or {}
    dangerous = set(config.get("tier2_surfaces") or [])
    if dangerous & set(surfaces):
        return 2
    trivial = set(config.get("tier0_patterns") or [])
    if patterns and set(patterns) <= trivial:
        return 0
    return 1


def _plan_gate(
    cartridge: Mapping[str, Any], bound: Mapping[str, Any], surfaces: list[str], patterns: list[str]
) -> tuple[int, int, bool, bool, bool]:
    """Whether the competition and the attack are worth their price on this task.

    Returns the tier, the configured floor, whether the competition PAIR is
    bound, whether the plan attacker is bound, and whether the floor is
    cleared. The two seats are reported separately on purpose: a cartridge that
    binds only the attacker never configured a competition, and must not be
    told one was skipped. The floor comparison is made exactly once, here.
    """
    tier = _plan_tier(cartridge, surfaces=surfaces, patterns=patterns)
    min_tier = int(((cartridge.get("policy") or {}).get("plan_competition") or {}).get("min_tier", 0))
    competition_bound = "plan_alternative" in bound and "plan_arbitrate" in bound
    attack_bound = "plan_adversary" in bound
    return tier, min_tier, competition_bound, attack_bound, tier >= min_tier


def _plan_attack(
    runner: NodeRunner,
    *,
    context: list[str],
    ticket: Any,
    author: _Author,
    plan: Mapping[str, Any],
    task_id: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The adversary, moved to the front of the loop.

    After `build`, an objection costs a rebuild. Before it, an objection costs
    one more plan. So the plan's claims — this file exists, this signature is
    what the test will call, this step can be checked without doing the next
    one — get attacked while they are still cheap to be wrong about.

    Bounded to ONE revision, by the plan's AUTHOR on the author's own thread:
    the first planner when its plan won or no competition ran, the second
    planner when the arbiter chose `second`, the arbiter itself when the plan
    is a merge. The objections travel verbatim. Sending a planner another
    planner's plan would hand it a thread already holding its own losing plan
    and call the result a revision. The revision is not attacked again: the
    review round after build is where the built plan gets judged, and a loop
    here would be a second fix loop with none of the first one's accounting.
    """
    attack = dict(
        runner.run(
            role="plan_adversary",
            task=str(task_id),
            hints=Hints(judgment="high"),
            schema=PLAN_ATTACK_SCHEMA,
            context=context,
            prompt=(
                "Your job is to disagree with this plan before anyone builds it. "
                "Attack the claims it rests on: a file or function it assumes "
                "exists, a signature it assumes, a step that cannot be checked, a "
                "step that depends on the next one, scope that has quietly grown. "
                "Check what you can against the repository.\n\n"
                f"Ticket: {ticket}\nPlan: {plan}\n\n"
                "State your strongest objection plainly, even if you conclude the "
                "plan can proceed."
            ),
        )
    )
    if attack.get("verdict") != "revise":
        return dict(plan), {"attack": attack, "revised": False}

    objections = "\n".join(
        f"- {objection.get('claim')} — {objection.get('why_wrong')}"
        for objection in attack.get("objections") or []
        if isinstance(objection, Mapping)
    )
    revised = dict(
        runner.run(
            role=author.role,
            tier=author.tier,
            thread=author.thread,
            task=str(task_id),
            schema=PLAN_SCHEMA,
            context=context,
            prompt=(
                "This plan was sent back before build. Revise it so that every "
                "objection below actually falls — a plan that leaves one standing "
                "is not a revision.\n\n"
                f"Ticket: {ticket}\nPlan: {plan}\n\n"
                f"Objections:\n{objections}\n"
                f"Strongest: {attack.get('strongest_objection')}\n\n"
                "Name the files you expect to touch, and state what is explicitly "
                "out of scope."
            ),
        )
    )
    return revised, {"attack": attack, "revised": True}


def _handoff(
    runner: NodeRunner,
    *,
    context: list[str],
    ticket: Any,
    plan: Mapping[str, Any],
    build: Mapping[str, Any],
    facts: Mapping[str, Any],
    ticket_id: Any = None,
) -> dict[str, Any]:
    """The shuttle. Between build and review, someone checks that what came out
    of the last step is actually what the next one needs — and stops here if it
    is not. A review of a half-finished change produces a confident opinion
    about the wrong thing.

    A retry gets exactly the same check as the first try. An incomplete second
    attempt is still incomplete, and "we were already fixing it" is not a reason
    to review a gap.

    But an incomplete handoff is not one thing. Stopping the line is right when
    the artifact or an input is missing, because no amount of rebuilding
    conjures an input nobody produced. It is wrong when the patch is there and
    what is missing is evidence about it: that is a gap one more build attempt
    closes, and quarantining it throws away a finished change and buys a whole
    replan to get back to where the run already was. Three of four handoff stops
    in one day were the second kind. So this raises only on the blocking kind,
    and returns the incomplete handoff otherwise for the caller to route into
    the fix loop.

    Whether the artifact is present is decided HERE, from the patch, and only
    then refined by the node's own flag: a shuttle cannot truthfully call an
    artifact missing while holding it, and a graph that took its word for it
    would be re-deciding a deterministic fact with a model call.
    """
    # The artifact travels with the question. An earlier version handed the
    # handoff only the summary, the file list and the line counts — and it
    # correctly refused every build for "no patch text was handed off", five
    # epics running. A shuttle that cannot see the cargo cannot judge it.
    patch = str(build.get("patch") or "")
    try:
        handoff = dict(
            runner.run(
                role="handoff",
                tier="standard",
                task=None if ticket_id is None else str(ticket_id),
                schema=HANDOFF_SCHEMA,
                context=context,
                prompt=(
                    "The build step is done and the review step is next. Does what "
                    "build produced actually contain what a reviewer needs?\n\n"
                    f"Task: {ticket}\nPlan: {plan}\nSummary: {build.get('summary')}\n"
                    f"Files: {build.get('files_touched')}\nChange facts: {facts}\n"
                    "The facts listed under Change facts are already measured by the "
                    "harness; do not list any of them as missing. Any size target in "
                    "the ticket binds source lines only — test lines are reported "
                    "here, never a missing item.\n"
                    f"Commands run (with their real output): {build.get('commands_run')}\n"
                    "Rows with source harness_verify were run by the harness in the build's own "
                    "scratch after the patch, not by the builder; they are the ticket's verify: evidence. "
                    "Both reviewers receive them verbatim.\n"
                    f"Patch ({len(patch)} chars, {'complete' if len(patch) <= PATCH_PREVIEW_CHARS else 'head shown'}):\n"
                    f"{patch[:PATCH_PREVIEW_CHARS]}\n\n"
                    "List anything missing, and compress the rest into the smallest "
                    "brief that lets review start. The patch above IS the artifact under "
                    "review: judge whether it and the command evidence are sufficient, not "
                    "whether a repository somewhere already contains them.\n\n"
                    "If it is not complete, say whether the refusal is BLOCKING. Blocking "
                    "means the artifact itself or an input the plan needed is absent — "
                    "there is no change here, or the work depended on something nobody "
                    "produced, and no amount of rebuilding will conjure it. Not blocking "
                    "means the change is present and what it lacks is evidence about it: "
                    "a check nobody ran, output nobody attached, a claim nobody tested. "
                    "That second kind buys one more build attempt; it does not stop the "
                    "line, so do not mark it blocking to signal that it matters."
                ),
            )
        )
    except BudgetStop:
        # A budget stop is not a node failure — the CLI can resume this
        # session in a later phase, the same continuation the build node
        # already gets. Left to propagate unconverted, so it still reaches
        # `invoke_graphs` and quarantines `no_work` exactly as before this
        # change; retrying a non-build node's session is a sibling ticket's.
        raise
    except RunnerError as exc:
        raise _NodeFailure("handoff", exc) from exc
    # Anything the model flagged as missing that the harness already measured
    # is discharged here, not argued with the model: the fact is not absent,
    # it is sitting in `facts` unread. If discharging clears the list, the
    # handoff was complete all along.
    kept, discharged = prune_missing(list(handoff.get("missing") or []), measured_facts(build, facts))
    # Evidence that asks for a command the build's session cannot run is not a
    # gap another build attempt can close; discharge it rather than loop on it.
    permitted = getattr(runner, "permitted_prefixes", None)
    prefixes = permitted() if callable(permitted) else []
    if prefixes:
        impossible = [item for item in kept if impossible_evidence(item, prefixes)]
        kept = [item for item in kept if item not in impossible]
        discharged = [*discharged, *(f"{item} (not runnable in the build's session)" for item in impossible)]
    handoff["missing"] = kept
    handoff["discharged"] = discharged
    if not kept:
        handoff["complete"] = True
        handoff["blocking"] = False
    source_lines = int(facts.get("source_lines") or 0)
    test_lines = int(facts.get("test_lines") or 0)
    deviations = [
        d
        for d in (_size_deviation(str(ticket), source_lines, test_lines), _test_ratio_deviation(source_lines, test_lines))
        if d
    ]
    if deviations:
        brief = str(handoff.get("brief") or "")
        handoff["brief"] = "\n".join([brief, *deviations]) if brief else "\n".join(deviations)
    # No patch is an absent artifact as a matter of fact, whatever the node
    # said; with a patch in hand, only the node knows whether an INPUT was
    # missing, so its flag decides.
    blocking = not patch.strip() or bool(handoff.get("blocking"))
    if not handoff.get("complete") and blocking:
        missing = ", ".join(handoff.get("missing") or []) or "unspecified"
        raise ContractViolation(
            f"handoff from build to review is incomplete for '{ticket_id if ticket_id is not None else ticket}': {missing}. "
            "The artifact or an input is missing, so the graph stops rather than "
            "reviewing a change that is not finished — a step that builds on a gap "
            "is how a phase goes quietly wrong."
        )
    return handoff


def _handoff_critique(
    handoff: Mapping[str, Any],
) -> tuple[dict[str, Any], None, None, str]:
    """A non-blocking handoff refusal, in the shape the fix loop already carries.

    The loop moves on a review verdict and a critique, so a handoff that refused
    for missing evidence enters it as exactly that: a `revise` whose findings are
    the handoff's own `missing` list, verbatim. No reviewer ran and none is
    invented — the adversary and the arbitration stay `None`, so nothing
    downstream can mistake the shuttle's objection for a second opinion about
    the code.
    """
    missing = [str(item) for item in handoff.get("missing") or [] if str(item).strip()]
    return (
        {
            "verdict": "revise",
            "findings": [
                {"charter_principle": "handoff evidence", "detail": item, "file": ""}
                for item in missing
            ],
            "rationale": str(handoff.get("brief") or "")
            or f"the handoff refused for missing evidence: {', '.join(missing) or 'unspecified'}",
        },
        None,
        None,
        "revise",
        False,
        False,
    )


def review_is_placeholder(answer: Mapping[str, Any]) -> bool:
    """Pure: does this review answer describe itself instead of judging the change?

    Reads REVIEW_SCHEMA's findings/rationale and ADVERSARY_SCHEMA's
    objections/strongest_objection by the same rule, so the charter and
    adversary reviewers share one check. True when every finding's detail IS
    (not merely contains) one of phase_validate's markers or the bare word
    "placeholder" — a substring search would also catch a real finding ABOUT
    placeholder code, which is the one thing this must never flag. True when
    `rationale` itself is empty, "{}", or only whitespace — REVIEW_SCHEMA
    requires it, so its absence is never legitimate; the check is scoped to
    that field alone, because ADVERSARY_SCHEMA has no `rationale` and an
    adversary that approves with nothing left to object to is not a
    placeholder. True when there are no findings or objections at all and the
    verdict is 'revise' with nothing said either — a revise with nothing
    behind it, on either schema.
    """
    def text(item: Mapping[str, Any]) -> str:
        return str(item.get("detail") or item.get("why_wrong") or item.get("claim") or "").strip().lower()

    items = list(answer.get("findings") or answer.get("objections") or [])
    summary = str(answer.get("rationale") or answer.get("strongest_objection") or "").strip()
    empty_summary = summary in ("", "{}")
    all_placeholders = bool(items) and all(text(item) == "placeholder" or text(item) in _PLACEHOLDER_MARKERS for item in items)
    empty_rationale = "rationale" in answer and str(answer.get("rationale") or "").strip() in ("", "{}")
    empty_revise = not items and answer.get("verdict") == "revise" and empty_summary
    return all_placeholders or empty_rationale or empty_revise


class _NodeFailure(RunnerError):
    """A non-build node raised mid-round; carries the role and whatever the
    round had already decided, so a caller that catches this can quarantine
    with patch and partial verdicts instead of losing both to the traceback.
    """

    def __init__(self, role: str, cause: Exception, *, review: Mapping[str, Any] | None = None,
                 adversary: Mapping[str, Any] | None = None) -> None:
        self.role = role
        self.review = review
        self.adversary = adversary
        super().__init__(f"node '{role}' failed: {cause}")


def _reviewer_answer(
    runner: NodeRunner,
    *,
    role: str,
    hints: Hints | None = None,
    model_tier: str | None = None,  # review_entry still passes a literal tier; its own task drops it.
    schema: Mapping[str, Any],
    context: list[str],
    prompt: str,
    task_id: Any = None,
) -> tuple[dict[str, Any], bool]:
    """One reviewer's answer, and one retry if it is a placeholder instead.

    Same shape as phase_validate._verdict: same role and prompt on retry, plus
    one sentence saying the previous answer did not count. Retried once and
    never more — a reviewer that will not answer twice abstains, and it is the
    caller's to decide what an abstention costs.
    """
    task = None if task_id is None else str(task_id)
    try:
        first = dict(
            runner.run(
                role=role, tier=model_tier, hints=hints, schema=schema, context=context, prompt=prompt, task=task
            )
        )
        if not review_is_placeholder(first):
            return first, False
        second = dict(
            runner.run(
                role=role,
                task=task,
                tier=model_tier,
                hints=hints,
                schema=schema,
                context=context,
                prompt=(
                    f"{prompt}\n\n"
                    "Your previous answer named what you would check instead of checking it — "
                    "a placeholder, not a verdict. Answer for real this time."
                ),
            )
        )
    except BudgetStop:
        raise  # same as `_handoff`: a resumable stop, not a node failure.
    except RunnerError as exc:
        raise _NodeFailure(role, exc) from exc
    return second, review_is_placeholder(second)


def _review_harness_fault() -> dict[str, Any]:
    """Pure: the review a double abstention becomes, never the placeholder verbatim.

    Built from REVIEW_SCHEMA's own keys. Handing the raw placeholder answer
    on as `review` would let its "placeholder" finding read, downstream, as a
    real one — the exact confusion this change exists to stop.
    """
    return {
        "verdict": "revise",
        "findings": [
            {
                "charter_principle": "harness fault",
                "detail": "both reviewers returned a placeholder twice and produced no judgment",
                "file": "",
            }
        ],
        "rationale": "harness fault: review placeholders",
    }


def _patch_truncated_review(reason: str) -> dict[str, Any]:
    """Pure: the review slot for a patch still truncated after its one retry."""
    return {
        "verdict": "revise",
        "findings": [{"charter_principle": "harness fault", "detail": reason, "file": ""}],
        "rationale": "patch_truncated",
    }


def _abstained_review() -> dict[str, Any]:
    """Pure: the charter reviewer's slot in the record once it has abstained.

    Its own words never reach `_critique` or a later build prompt — only the
    fact of the abstention does. Forwarding the raw second answer would put
    the 2026-09-05 text back in front of a builder under a different name.
    """
    return {"verdict": "revise", "findings": [], "rationale": "review_charter did not produce a judgment after a second attempt"}


def _abstained_adversary() -> dict[str, Any]:
    """Pure: the adversary's slot in the record once it has abstained, same rule."""
    return {
        "verdict": "revise",
        "objections": [],
        "strongest_objection": "review_adversary did not produce a judgment after a second attempt",
    }


ARBITER_SKIPPED = "arbiter: skipped (both approved)"


def should_skip_arbiter(charter_verdict: str, adversary_verdict: str) -> bool:
    """True only when both reviewers approved. Revise with revise is over-strict often enough to need the arbiter."""
    return charter_verdict == "approve" and adversary_verdict == "approve"


def review_depth(answer: Answer | None, threshold: float) -> Literal["cheap", "deep"]:
    """cheap only for a confident approve; revise, reject, low confidence and no answer are all deep."""
    confident_approve = answer is not None and answer.value == "approve" and answer.confidence >= threshold
    return "cheap" if confident_approve else "deep"


def _consult_prescreen(runner: NodeRunner, request: Mapping[str, Any]) -> tuple[Literal["cheap", "deep"], str] | None:
    """Edge: the pre-screen's depth and its role's mode, or None when the runner has no pre-screen.

    The depth is computed in shadow and in on alike; `_charter_judgment` decides which one acts on it.
    """
    consult = getattr(runner, "consult", None)
    consulted = consult("review_charter", request) if callable(consult) else None
    if consulted is None:
        return None
    answer, setting = consulted
    return review_depth(answer, setting.threshold), setting.mode


def _charter_judgment(prescreen: tuple[Literal["cheap", "deep"], str] | None) -> Literal["low"] | None:
    """Only a live cheap depth lowers the charter reviewer's hints; shadow and deep leave today's call."""
    return "low" if prescreen == ("cheap", "on") else None


def _review_round(
    runner: NodeRunner,
    *,
    context: list[str],
    bound: Mapping[str, Any],
    ticket: Any,
    build: Mapping[str, Any],
    facts: Mapping[str, Any],
    handoff: Mapping[str, Any] | None,
    tier: int,
    attempt: int,
    task_id: Any = None,
) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any] | str | None, str, bool, bool]:
    """One full round of review, and the verdict it reaches.

    Factored out because a retry is reviewed under EXACTLY the same rules as the
    first try — same tier arithmetic, same optional roles, same arbitration
    trigger. A fix loop with a cheaper second pass would be a way of grinding a
    change past its reviewers, which is the thing this loop must not become.

    The fifth element is true when either reviewer placeholdered twice; the
    sixth is true only when BOTH did, which the caller quarantines rather than
    sends back to build — a reviewer that abstains is not a verdict to rebuild
    against.
    """
    # The call declares its role and hints; the resolver picks the model tier.
    patch = str(build.get("patch") or "")
    task = None if task_id is None else str(task_id)
    review_hints = _hints(patch, attempt=attempt)
    arbiter_hints = _hints(patch, attempt=attempt, judgment="high")
    charter_prompt = (
        "Review this change against the team's own written charter in your "
        f"context.\n\nTask: {ticket}\nSummary: {build.get('summary')}\n"
        f"Change facts: {facts}\n"
        + (f"Handoff brief: {handoff.get('brief')}\n" if handoff else "")
        + harness_verify_block(build)
        + f"Patch:\n{build.get('patch')}\n\n"
        "Cite the charter principle behind every finding."
    )
    # The pre-screen lowers only the charter reviewer's hints, and only in mode "on". The adversary
    # keeps today's hints: its job is to disagree with the approve the pre-screen is confident about.
    prescreen = _consult_prescreen(runner, {"role": "review_charter", "prompt": charter_prompt})
    review, charter_abstained = _reviewer_answer(
        runner,
        role="review_charter",
        hints=_hints(patch, attempt=attempt, judgment=_charter_judgment(prescreen)),
        schema=REVIEW_SCHEMA,
        context=context,
        prompt=charter_prompt,
        task_id=task_id,
    )

    # Tier 0 is the cheapest review, never the absence of one.
    adversary: dict[str, Any] | None = None
    adversary_abstained = False
    if tier >= 1 and "review_adversary" in bound:
        try:
            adversary, adversary_abstained = _reviewer_answer(
                runner,
                role="review_adversary",
                hints=review_hints,
                schema=ADVERSARY_SCHEMA,
                context=context,
                prompt=(
                    "Your job is to disagree. Find what this change gets wrong, and "
                    "what the first reviewer accepted too easily.\n\n"
                    f"Task: {ticket}\nChange facts: {facts}\n"
                    f"First reviewer said: {review.get('verdict')} — {review.get('rationale')}\n"
                    f"{harness_verify_block(build)}"
                    f"Patch:\n{build.get('patch')}\n\n"
                    "State your strongest objection plainly, even if you end up approving."
                ),
                task_id=task_id,
            )
        except _NodeFailure as exc:
            # The charter reviewer already answered by the time the adversary
            # call raises — attach it, or a mid-round failure here would lose
            # a verdict this same function already has in hand.
            exc.review = dict(review)
            raise

    # An abstained reviewer is dropped from the record and the critique alike —
    # its raw second answer never reaches a builder — and the survivor's
    # verdict decides, through arbitration when the team has bound one, so a
    # tier that would have demanded a third read still gets one. Neither
    # survives, and there is nothing to decide with — the caller quarantines
    # that.
    try:
        charter_usable, adversary_usable = not charter_abstained, adversary is not None and not adversary_abstained
        if not charter_usable and not adversary_usable:
            return _review_harness_fault(), adversary, None, "revise", True, True
        if not charter_usable:
            sole_arbitration = None
            if "arbitrate" in bound:
                sole_arbitration = dict(
                    runner.run(
                        role="arbitrate",
                        task=task,
                        hints=arbiter_hints,
                        schema=ARBITRATE_SCHEMA,
                        context=context,
                        prompt=(
                            "One reviewer has looked at this change; the other did not produce a "
                            "judgment after a second attempt and has abstained. Decide.\n\n"
                            f"Task: {ticket}\nReview tier: {tier}\n"
                            "Charter reviewer: abstained, no judgment\n"
                            f"Adversary: {adversary.get('verdict')} — {adversary.get('strongest_objection')}\n"
                            f"Change facts: {facts}\n\n"
                            "Say who you sided with and why. 'neither' is allowed."
                        ),
                    )
                )
            sole_verdict = str(sole_arbitration.get("verdict")) if sole_arbitration is not None else str(adversary.get("verdict"))
            return _abstained_review(), adversary, sole_arbitration, sole_verdict, True, False
        if not adversary_usable and adversary is not None:
            sole_arbitration = None
            if "arbitrate" in bound:
                sole_arbitration = dict(
                    runner.run(
                        role="arbitrate",
                        task=task,
                        hints=arbiter_hints,
                        schema=ARBITRATE_SCHEMA,
                        context=context,
                        prompt=(
                            "One reviewer has looked at this change; the other did not produce a "
                            "judgment after a second attempt and has abstained. Decide.\n\n"
                            f"Task: {ticket}\nReview tier: {tier}\n"
                            f"Charter reviewer: {review.get('verdict')} — {review.get('rationale')}\n"
                            "Adversary: abstained, no judgment\n"
                            f"Change facts: {facts}\n\n"
                            "Say who you sided with and why. 'neither' is allowed."
                        ),
                    )
                )
            sole_verdict = str(sole_arbitration.get("verdict")) if sole_arbitration is not None else str(review.get("verdict"))
            return dict(review), _abstained_adversary(), sole_arbitration, sole_verdict, True, False

        # Arbitration on disagreement, and at tier 2 unless both approved. Measured
        # over 383 records, approve with approve was upheld 83 of 83; revise with
        # revise was overturned 12 of 82, so only the approving pair skips.
        arbitration: dict[str, Any] | str | None = None
        disagreed = adversary is not None and adversary.get("verdict") != review.get("verdict")
        skip = adversary is not None and should_skip_arbiter(str(review.get("verdict")), str(adversary.get("verdict")))
        if "arbitrate" in bound and skip and tier == 2:
            arbitration = ARBITER_SKIPPED
        elif "arbitrate" in bound and adversary is not None and (disagreed or tier == 2):
            arbitration = dict(
                runner.run(
                    role="arbitrate",
                    task=task,
                    hints=arbiter_hints,
                    schema=ARBITRATE_SCHEMA,
                    context=context,
                    prompt=(
                        "Two reviewers have looked at this change. Decide.\n\n"
                        f"Task: {ticket}\nReview tier: {tier}\n"
                        f"Charter reviewer: {review.get('verdict')} — {review.get('rationale')}\n"
                        f"Adversary: {adversary.get('verdict')} — {adversary.get('strongest_objection')}\n"
                        f"Change facts: {facts}\n\n"
                        "Say who you sided with and why. 'neither' is allowed."
                    ),
                )
            )
    except BudgetStop:
        raise  # same as `_handoff`: a resumable stop, not a node failure.
    except RunnerError as exc:
        raise _NodeFailure("arbitrate", exc, review=dict(review), adversary=adversary) from exc

    # The last word: arbitration if it ran, otherwise both reviewers must agree.
    # Silence from an unbound optional role is not an approval, but neither is it
    # an objection — an unbound adversary simply leaves the charter reviewer
    # deciding, exactly as before.
    if isinstance(arbitration, Mapping):
        verdict = str(arbitration.get("verdict"))
    elif arbitration == ARBITER_SKIPPED:
        verdict = "approve"
    elif adversary is not None:
        verdict = "approve" if review.get("verdict") == adversary.get("verdict") == "approve" else "revise"
    else:
        verdict = str(review.get("verdict"))

    return dict(review), adversary, arbitration, verdict, False, False


def _infra_result(
    *, run_id: Any, date: Any, ticket: Any, scope: Mapping[str, Any] | None,
    build: Mapping[str, Any], handoff: Mapping[str, Any] | None, exc: _NodeFailure,
) -> dict[str, Any]:
    """The record for a task whose non-build node raised mid-round.

    Nothing here was judged, so there is no verdict and no proposal — only
    what survived the raise: the patch, whatever review the round reached
    before it failed, and which node failed.
    """
    return {
        "run_id": run_id,
        "date": date,
        "ticket": ticket,
        "scope": scope,
        "handoff": handoff,
        "adversary": exc.adversary,
        "arbitration": None,
        "build": dict(build),
        "review": exc.review,
        "proposals": [],
        "failed_node": exc.role,
        "failed_node_reason": str(exc),
    }


def run(
    args: Mapping[str, Any], runner: NodeRunner, decision_source: DecisionSource | None = None
) -> dict[str, Any]:
    """Run the graph. Every input arrives as an argument — no clock, no disk.

    `decision_source`, when given, is asked at every runner call. Without one
    no call carries a router decision.
    """
    ticket_tiers = dict(args.get("tier") or {})
    asking = runner if decision_source is None else _Asking(runner, decision_source)
    return _run(args, _Elevated(asking, ticket_tiers) if ticket_tiers else asking, ticket_tiers)


def _run(args: Mapping[str, Any], runner: NodeRunner, ticket_tiers: Mapping[str, str]) -> dict[str, Any]:
    """The graph body; `runner` already carries the ticket's tiers."""
    cartridge = require_cartridge(args)
    run_id, date, ticket = require(args, "run_id", "date", "ticket")
    # The work item's own words travel with its id. Traced plan nodes spent
    # their first turns globbing the repository for a file named like the
    # ticket, because the id was all they were given; a title and a body in
    # the prompt is the difference between a 10-turn plan and a 24-turn one.
    ticket_text = _ticket_text(ticket, args.get("ticket_title"), args.get("ticket_body"))

    # A per-call dollar ceiling for the build role. The harness has no
    # "float" kind (`Need` offers str, int, json_file, jsonl_file,
    # text_or_path — see graphs/_spec.py), so a CLI-supplied value arrives as
    # a raw string; the epic driver hands one straight off parsed YAML as a
    # float already. Coerced once, here, rather than trusted at each site
    # that passes it to the runner.
    raw_build_budget_usd = args.get("build_budget_usd")
    build_budget_usd = None if raw_build_budget_usd is None else float(raw_build_budget_usd)

    is_work_item = bool(args.get("work_item"))

    context = list(cartridge.get("context") or [])
    proposals: list[dict[str, Any]] = []

    # Scoping is a SEPARATE ACT from filing, and it runs first: unscoped work
    # routes to the future-work landing area, never onto the active board.
    # Optional — a team that has not bound `scope_epic` simply does not get it,
    # which is what an optional role means.
    scope: dict[str, Any] | None = None
    if "scope_epic" in (cartridge.get("skills") or {}):
        scope = dict(
            runner.run(
                role="scope_epic",
                tier="standard",
                schema=SCOPE_SCHEMA,
                context=context,
                prompt=(
                    f"Scope this work.\n\nTicket: {ticket_text}\nDate: {date}\n\n"
                    "List the phases, the tickets, and the repositories it touches. "
                    "Say whether it is being worked now (active), scoped and scheduled "
                    "(planned), or roadmapped for later (future). Name an existing epic "
                    "to attach to if one covers this area."
                ),
            )
        )
        shape = epic_shape(
            cartridge,
            phases=len(scope.get("phases") or []),
            tickets=len(scope.get("tickets") or []),
            repos=len(scope.get("repos") or []),
        )
        landing = landing_for(cartridge, scope.get("state", "planned"))
        scope["shape"] = shape
        scope["landing"] = landing

        # A ticket that is already a work item must not propose filing itself: the
        # workstore arm has no apply block to route, falls back, and writes nothing.
        if not is_work_item:
            proposals.append(
                proposal(
                    cartridge,
                    kind="item_create",
                    target=str(scope.get("parent_epic") or ticket),
                    evidence=[
                        {"check": "epic_threshold", "output": f"{shape} ({len(scope.get('tickets') or [])} tickets, {len(scope.get('phases') or [])} phases, {len(scope.get('repos') or [])} repos)"},
                        {"check": "work_routing", "output": f"state '{scope.get('state')}' lands in {landing}"},
                    ],
                    rationale=str(scope.get("rationale", "")),
                    suggested_action=(
                        f"file as {shape} in {landing}"
                        + (f", attached to {scope['parent_epic']}" if scope.get("parent_epic") else "")
                    ),
                )
            )

    bound = cartridge.get("skills") or {}
    surfaces = list(args.get("surfaces") or [])
    patterns = list(args.get("patterns") or [])
    gate_tier, gate_min, competition_bound, attack_bound, compete = _plan_gate(cartridge, bound, surfaces, patterns)
    plan_gate = {
        "tier": gate_tier,
        "min_tier": gate_min,
        "competition": competition_bound and compete,
        "attack": attack_bound and compete,
        "ran": (competition_bound or attack_bound) and compete,
    }

    # The first planner keeps the ticket's own thread; build joins it later.
    author = _Author("plan", "standard", str(ticket))
    first_plan = runner.run(
        role=author.role,
        tier=author.tier,
        thread=author.thread,
        task=str(ticket),
        schema=PLAN_SCHEMA,
        context=context,
        prompt=(
            f"Decompose this ticket into an ordered plan.\n\nTicket: {ticket_text}\n"
            f"Date: {date}\n\nName the files you expect to touch, and state what is "
            "explicitly out of scope."
        ),
    )

    # The competition needs both halves: an alternative nobody judges is a
    # plan nobody builds, and an arbiter with one plan has nothing to decide.
    competition: dict[str, Any] | None = None
    if "plan_alternative" in bound and "plan_arbitrate" in bound and compete:
        chosen_plan, competition, author = _plan_competition(
            runner, context=context, ticket=ticket_text, date=date, plan=first_plan, first=author, task_id=ticket
        )
    else:
        chosen_plan = dict(first_plan)

    # A revision goes to whoever wrote the chosen plan, on that seat's thread.
    plan_attack: dict[str, Any] | None = None
    if "plan_adversary" in bound and compete:
        plan, plan_attack = _plan_attack(
            runner, context=context, ticket=ticket_text, author=author, plan=chosen_plan, task_id=ticket
        )
    else:
        plan = chosen_plan

    # Plan, build and the fix-loop retry share one thread: the builder starts
    # from what the planner already read, and a retry from a tree it already
    # edited. Review never joins the thread — a reviewer that inherits the
    # builder's reasoning is the failure the seat exists to prevent.
    continuations = 0
    continuation_refused: str | None = None
    try:
        build = runner.run(
            role="build",
            tier="standard",
            thread=str(ticket),
            task=str(ticket),
            schema=BUILD_SCHEMA,
            context=context,
            budget_usd=build_budget_usd,
            prompt=(
                f"Carry out this plan and return the change as a unified diff.\n\n"
                f"Ticket: {ticket_text}\nPlan: {plan}\n\nReturn the patch only — it is applied "
                "by the shell into a worktree, never by you. No tags, no fences, no trailing "
                "markup of any kind — the text is fed to `git apply` verbatim and a stray "
                "`</patch>` fails the checks. Include the deterministic "
                "commands you ran and their output."
            ),
        )
    except BudgetStop as exc:
        resumed, continuations, reason, stop = _resume_build(
            runner, context=context, ticket=ticket, budget_usd=build_budget_usd,
            surfaces=surfaces, stop=exc, continuations=continuations,
        )
        if resumed is None:
            # There is no reviewed build yet to keep — nothing to idle. The
            # reason travels on the exception itself, since no result is
            # returned for a `continuation_refused` field to live on.
            raise BudgetStop(
                role=stop.role,
                thread=stop.thread,
                session=stop.session,
                spent_usd=stop.spent_usd,
                partial_patch=stop.partial_patch,
                detail=f"{stop.detail} — continuation refused: {reason}",
            ) from stop
        build = resumed

    # A patch that does not parse is asked again once, with the reason
    # attached, and never chased further: the second answer is final.
    truncation = patch_parses(build.get("patch") or "")
    if truncation is not None:
        try:
            build = runner.run(
                role="build",
                tier="standard",
                thread=str(ticket),
                task=str(ticket),
                schema=BUILD_SCHEMA,
                context=context,
                budget_usd=build_budget_usd,
                prompt=(
                    f"Carry out this plan and return the change as a unified diff.\n\n"
                    f"Ticket: {ticket_text}\nPlan: {plan}\n\nReturn the patch only — it is applied "
                    "by the shell into a worktree, never by you. No tags, no fences, no trailing "
                    "markup of any kind — the text is fed to `git apply` verbatim and a stray "
                    "`</patch>` fails the checks. Include the deterministic "
                    f"commands you ran and their output. {truncation}"
                ),
            )
        except BudgetStop as exc:
            resumed, continuations, reason, stop = _resume_build(
                runner, context=context, ticket=ticket, budget_usd=build_budget_usd,
                surfaces=surfaces, stop=exc, continuations=continuations,
            )
            if resumed is None:
                raise BudgetStop(
                    role=stop.role,
                    thread=stop.thread,
                    session=stop.session,
                    spent_usd=stop.spent_usd,
                    partial_patch=stop.partial_patch,
                    detail=f"{stop.detail} — continuation refused: {reason}",
                ) from stop
            build = resumed
        truncation = patch_parses(build.get("patch") or "")
    patch_truncated = truncation is not None

    # A patch whose files diverge from `files_touched`, or whose contract
    # commands are missing or unevidenced, is asked again once, named.
    invalid = None if patch_truncated else _build_output_valid(build, ticket_text)
    if invalid is not None:
        try:
            build = runner.run(
                role="build", hints=_hints(str(build.get("patch") or "")),
                thread=str(ticket), task=str(ticket), schema=BUILD_SCHEMA,
                context=context, budget_usd=build_budget_usd,
                prompt=(
                    f"Carry out this plan and return the change as a unified diff.\n\n"
                    f"Ticket: {ticket_text}\nPlan: {plan}\n\n{invalid} Return the patch only — it is "
                    "applied by the shell into a worktree, never by you."
                ),
            )
        except BudgetStop as exc:
            resumed, continuations, reason, stop = _resume_build(
                runner, context=context, ticket=ticket, budget_usd=build_budget_usd,
                surfaces=surfaces, stop=exc, continuations=continuations,
            )
            if resumed is None:
                raise BudgetStop(
                    role=stop.role, thread=stop.thread, session=stop.session, spent_usd=stop.spent_usd,
                    partial_patch=stop.partial_patch, detail=f"{stop.detail} — continuation refused: {reason}",
                ) from stop
            build = resumed
        invalid = _build_output_valid(build, ticket_text)
    build_output_invalid = invalid is not None
    frozen = patch_truncated or build_output_invalid

    facts = _change_facts(build)
    tier = review_tier(cartridge, change_facts=facts, surfaces=surfaces, patterns=patterns)

    handoff: dict[str, Any] | None = None
    # Declared here, not beside `standing`: the first pass below appends to it.
    rounds: list[dict[str, Any]] = []
    try:
        if not frozen and "handoff" in bound:
            handoff = _handoff(runner, context=context, ticket=ticket_text, plan=plan, build=build, facts=facts, ticket_id=ticket)

        # A non-blocking refusal costs a build attempt, not the run. Review is
        # skipped — there is nothing yet worth a deep-tier opinion — and the
        # handoff's own list of what is missing is what the builder is sent back
        # with. Paying two reviewers to read a change the shuttle already said is
        # under-evidenced would buy an opinion about the wrong thing. A patch still
        # truncated after its one retry never buys a review either — there is
        # nothing yet that applies.
        if patch_truncated:
            review, adversary, arbitration = _patch_truncated_review(truncation), None, None
            verdict, review_placeholder, review_quarantine = "revise", False, False
        elif build_output_invalid:
            review = {"verdict": "revise", "rationale": "build_output_invalid",
                      "findings": [{"charter_principle": "harness fault", "detail": invalid, "file": ""}]}
            adversary, arbitration, verdict, review_placeholder, review_quarantine = None, None, "revise", False, False
        elif handoff is not None and not handoff.get("complete"):
            review, adversary, arbitration, verdict, review_placeholder, review_quarantine = _handoff_critique(handoff)
            rounds.append(round_summary(1, "handoff", review, adversary, arbitration, verdict))
        else:
            review, adversary, arbitration, verdict, review_placeholder, review_quarantine = _review_round(
                runner,
                context=context,
                bound=bound,
                ticket=ticket_text,
                build=build,
                facts=facts,
                handoff=handoff,
                tier=tier,
                attempt=1,
                task_id=ticket,
            )
            rounds.append(round_summary(1, "review", review, adversary, arbitration, verdict))
    except _NodeFailure as exc:
        return _infra_result(run_id=run_id, date=date, ticket=ticket, scope=scope, build=build, handoff=handoff, exc=exc)
    # Carried across every round: an abstention two rounds ago is still an
    # abstention, even once a later round comes back clean.
    any_review_placeholder = review_placeholder

    # The bounded fix loop. A change sent back goes back to the builder with the
    # critique attached — but the loop is bounded in three separate ways, because
    # an unbounded one is just a machine for grinding a change past its reviewers
    # until someone blinks.
    fix_attempts = args.get("fix_attempts")
    fix_attempts = DEFAULT_FIX_ATTEMPTS if fix_attempts is None else int(fix_attempts)
    attempts = 1
    # Both reviewers abstaining is a harness fault, not a task to rebuild
    # against — the loop never gets a chance to start. A patch truncated twice
    # is the same shape: nothing to send back and revise.
    stopped: str | None = (
        "patch_truncated" if patch_truncated
        else "build_output_invalid" if build_output_invalid
        else "harness fault: review placeholders" if review_quarantine
        else None
    )
    standing: set[str] = set()
    # Whether the round that produced the current `verdict` ended at handoff
    # (no reviewer has read this patch yet) rather than at a review verdict.
    prior_handoff = handoff is not None and not handoff.get("complete")
    # Set when the second build asks a tier up. The runner takes no `reason`,
    # so the escalation is told on the record, as an evidence row, not to the call.
    escalation: str | None = None

    while verdict != "approve" and attempts <= fix_attempts and not review_quarantine and not frozen:
        # Every claim raised so far, not merely the last round's. Re-raising an
        # objection from two rounds ago is no more progress than re-raising the
        # one from the last.
        standing |= _claims(adversary)
        critique = _critique(review, adversary, arbitration)

        # Only the second build, and only after a review revise: not a handoff
        # gap and never a third attempt. A ticket tier for build overrides it.
        escalate = int(attempts) == 1 and verdict == "revise" and not prior_handoff and "build" not in ticket_tiers
        retry_tier = _escalate("standard") if escalate else "standard"
        if escalate:
            escalation = f"escalated: attempt 2 after revise (standard -> {retry_tier})"

        try:
            retry = runner.run(
                role="build",
                tier=retry_tier,
                thread=str(ticket),
                task=str(ticket),
                schema=BUILD_SCHEMA,
                context=context,
                budget_usd=build_budget_usd,
                prompt=(
                    "This change was sent back. Start from the previous patch — apply it "
                    "first, then change only what the critique requires — and return a new "
                    "unified diff of the whole change.\n\n"
                    f"Ticket: {ticket_text}\nPlan: {plan}\n\n"
                    f"Previous patch (apply this first; do not redo the work it already did):\n"
                    f"{build.get('patch')}\n\n"
                    f"Standing critique:\n{critique}\n\n"
                    "Every objection above must actually fall — a patch that leaves one "
                    "of them standing is not a fix, and saying it is addressed is not the "
                    "same as addressing it. Return the patch only — it is applied by the "
                    "shell into a worktree, never by you. No tags, no fences, no trailing "
                    "markup of any kind — the text is fed to `git apply` verbatim and a "
                    "stray `</patch>` fails the checks. Include the deterministic "
                    "commands you ran and their output."
                ),
            )
        except BudgetStop as exc:
            resumed, continuations, reason, _stop = _resume_build(
                runner, context=context, ticket=ticket, budget_usd=build_budget_usd,
                surfaces=surfaces, stop=exc, continuations=continuations, tier=retry_tier,
            )
            if resumed is None:
                # The retry spent the budget without returning a patch, and a
                # continuation was refused. `build` and `review` still
                # describe the last patch actually reviewed — that is the
                # thing worth keeping, not an exception that loses it along
                # with everything the run already earned.
                attempts += 1
                stopped = "budget"
                continuation_refused = reason
                break
            retry = resumed
        attempts += 1

        # No progress: a revise arbitration is judged by whether its scoped
        # files' own diff text moved, not the whole patch; no scope means any file.
        scope = _arbiter_scope(verdict, arbitration)
        if scope is None:
            no_progress = SequenceMatcher(None, build.get("patch") or "", retry.get("patch") or "").ratio() >= NO_PROGRESS_RATIO
        else:
            prior, current = _patch_sections(build.get("patch") or ""), _patch_sections(retry.get("patch") or "")
            # A scope naming files neither patch touches would zero progress
            # on nothing; only the scoped files that exist in a patch count,
            # and none left means any file.
            touched = (scope & (set(prior) | set(current)) if scope else set()) or set(prior) | set(current)
            no_progress = not any(prior.get(f) != current.get(f) for f in touched)
        # A resubmission after handoff evidence, not a review revise, is progress
        # the moment the evidence itself moved, even with the diff unchanged.
        if prior_handoff and no_progress:
            no_progress = retry.get("commands_run") == build.get("commands_run") and retry.get("summary") == build.get("summary")
        if no_progress:
            # The retry is dropped rather than returned: `build` and `review`
            # must describe the same patch, or the record lies about what was
            # reviewed. The attempt is still counted — it was still spent.
            stopped = "no_progress"
            break

        build = retry
        facts = _change_facts(build)
        try:
            if "handoff" in bound:
                handoff = _handoff(runner, context=context, ticket=ticket_text, plan=plan, build=build, facts=facts, ticket_id=ticket)
                # Still under-evidenced. The same rule as the first pass: another
                # attempt if the cap allows one, and never a review round bought
                # for a change the shuttle has already refused to hand over.
                if not handoff.get("complete"):
                    review, adversary, arbitration, verdict, review_placeholder, review_quarantine = _handoff_critique(handoff)
                    rounds.append(round_summary(int(attempts), "handoff", review, adversary, arbitration, verdict))
                    any_review_placeholder = any_review_placeholder or review_placeholder
                    prior_handoff = True
                    # No reviewer has seen this build yet, so the round costs
                    # half an attempt, not a whole one — two of these plus a
                    # real review round must not exhaust the cap first.
                    attempts -= 0.5
                    continue
            prior_handoff = False
            tier = review_tier(cartridge, change_facts=facts, surfaces=surfaces, patterns=patterns)
            review, adversary, arbitration, verdict, review_placeholder, review_quarantine = _review_round(
                runner,
                context=context,
                bound=bound,
                ticket=ticket_text,
                build=build,
                facts=facts,
                handoff=handoff,
                tier=tier,
                attempt=int(attempts),
                task_id=ticket,
            )
            rounds.append(round_summary(int(attempts), "review", review, adversary, arbitration, verdict))
        except _NodeFailure as exc:
            return _infra_result(run_id=run_id, date=date, ticket=ticket, scope=scope, build=build, handoff=handoff, exc=exc)
        any_review_placeholder = any_review_placeholder or review_placeholder
        if review_quarantine:
            stopped = "harness fault: review placeholders"
            break

        # An approval here is not a technicality. The reviewers saw the standing
        # objections in the patch they were given and approved anyway, which is
        # them judging the objections fallen. Their call, not the loop's.
        if verdict == "approve":
            break

        if standing & _claims(adversary):
            stopped = "objection_standing"
            break

    if verdict != "approve" and stopped is None:
        stopped = "attempts_exhausted"

    if verdict == "approve":
        # A draft PR has no effect until someone opens it, which is why it is the
        # one kind that starts eligible. It is still emitted, never executed.
        proposals.append(
            proposal(
                cartridge,
                kind="draft_pr_create",
                target=str(ticket),
                evidence=[
                    {"check": "review tier", "output": str(tier)},
                    # Only when a gated seat is bound but the tier never cleared
                    # the floor: a row that always reads "ran" is a row nobody
                    # reads, and a row present whether or not a seat is bound
                    # would claim a skip that was never even offered.
                    # One row per seat that was bound and skipped, named for what
                    # it is: a cartridge with only the attacker bound never had
                    # a competition to skip.
                    *(
                        [{"check": "plan gate", "output": f"tier {gate_tier} vs min {gate_min}: competition skipped"}]
                        if competition_bound and not compete
                        else []
                    ),
                    *(
                        [{"check": "plan gate", "output": f"tier {gate_tier} vs min {gate_min}: plan attack skipped"}]
                        if attack_bound and not compete
                        else []
                    ),
                    # Only when a competition or an attack ran: a row that
                    # always reads "no competition" is a row nobody reads.
                    *(
                        [{"check": "plan competition", "output": f"chose {competition['chosen']}: {competition['reasoning']} (price: {competition['price']})"}]
                        if competition
                        else []
                    ),
                    *(
                        [{"check": "plan adversary", "output": f"{plan_attack['attack'].get('verdict')} — strongest: {plan_attack['attack'].get('strongest_objection')}" + (" — plan revised once" if plan_attack["revised"] else "")}]
                        if plan_attack
                        else []
                    ),
                    {"check": "review_charter verdict", "output": str(review.get("verdict"))},
                    *(
                        [{"check": "adversary verdict", "output": str(adversary.get("verdict"))},
                         {"check": "strongest objection", "output": str(adversary.get("strongest_objection"))}]
                        if adversary
                        else []
                    ),
                    *(
                        [{"check": "arbitration", "output": f"{arbitration.get('sided_with')}: {arbitration.get('reasoning')}"}]
                        if isinstance(arbitration, Mapping) and arbitration
                        else [{"check": "arbitration", "output": arbitration}] if arbitration
                        else []
                    ),
                    # Only when there was a loop. A first-try approval says
                    # nothing about a fix loop because there was not one, and a
                    # row reading "attempt 1 of 3" on every clean pass is a row
                    # that stops being read.
                    *(
                        [{"check": "fix loop", "output": f"approved on attempt {attempts} of {fix_attempts + 1}"}]
                        if attempts > 1
                        else []
                    ),
                    *([{"check": "tier escalation", "output": escalation}] if escalation else []),
                    {"check": "changed lines", "output": str(facts["changed_lines"])},
                    # Only when the caller overrode the build budget: a row
                    # that always reads the default budget is a row nobody
                    # reads, and present whether or not an override was given
                    # would claim an override that never happened.
                    *(
                        [{"check": "build budget", "output": f"override ${build_budget_usd} per build call"}]
                        if build_budget_usd is not None
                        else []
                    ),
                    # Normalised into the evidence shape rather than spread raw:
                    # a commands_run entry is keyed `command`, and everything
                    # downstream — the gate, the manifest — reads `check`.
                    *(
                        {"check": entry.get("command"), "output": entry.get("output")}
                        for entry in build.get("commands_run", [])
                        if isinstance(entry, Mapping)
                    ),
                ],
                rationale=review.get("rationale", ""),
                suggested_action=f"open a draft PR for {ticket} from the build worktree",
                # Carried only when it happened, and then always. The ledger
                # cannot refuse to extend a streak on a repeated-attempt pass if
                # the pass never told it there was one.
                attempts=attempts if attempts > 1 else None,
            )
        )

    return {
        "run_id": run_id,
        "date": date,
        "ticket": ticket,
        "scope": scope,
        "review_tier": tier,
        "handoff": handoff,
        "adversary": adversary,
        "arbitration": arbitration,
        "plan": dict(plan),
        "plan_competition": competition,
        "plan_attack": plan_attack,
        "plan_gate": plan_gate,
        "build": dict(build),
        "review": dict(review),
        "change_facts": facts,
        "fix_loop": {
            "attempts": attempts,
            "stopped": stopped,
            "continuations": continuations,
            "rounds": rounds,
            **({"continuation_refused": continuation_refused} if continuation_refused is not None else {}),
            **({"review_placeholder": True} if any_review_placeholder else {}),
        },
        "proposals": proposals,
    }


from graphs._spec import GraphSpec, Need  # noqa: E402

SPEC = GraphSpec(
    name="lifecycle",
    graph_name=GRAPH_NAME,
    run=run,
    summary="the development loop: scope, plan, build, review — proposals out, nothing pushed",
    needs=(
        Need("ticket", flag="--ticket", help="the ticket to work"),
        Need("fix_attempts", flag="--fix-attempts", kind="int", required=False,
             help="additional build attempts after the first (default 2); 0 disables the fix loop"),
        Need("build_budget_usd", flag="--build-budget-usd", required=False,
             help="a per-call dollar ceiling for the build role, overriding the default "
                  "(a plain number; there is no float kind, so it arrives as a string "
                  "and this graph coerces it)"),
    ),
)
