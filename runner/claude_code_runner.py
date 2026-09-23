"""A runner that executes nodes through headless Claude Code: `claude -p`.

The Messages API runner needs an API key and can give a node nothing but a
prompt. This one needs a Claude Code login and can give a node *tools* — a
read-only view of the repository for the roles that plan, build and review, and
a write view of the work store for the apply arms. Same protocol, same graphs,
same tests; the difference is who pays and what a node can see.

Two things it does NOT change:

-   **The harness still applies every write.** A build node here can read the
    repository, so its diff is real rather than imagined — but it returns the
    diff, and the harness applies it in a worktree the harness owns. Tools are
    granted per ROLE from the provider profile, read-only by default, and only
    the arms get Write/Edit, scoped to the work store under the working
    directory. A node that is not named in the profile runs with no tools at
    all, which is exactly the API runner's contract.
-   **Nothing is read on this side of the boundary except what the profile and
    the harness hand over.** The skill body and context packs come in as paths
    the harness resolved; this module reads them at the edge, the same as the
    API runner does.

`repo_dir` is a plain attribute on purpose. The epic driver points it at the
phase worktree before each phase runs, so a node reads the branch it is about
to change rather than whatever the repository happens to have checked out.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from runner.protocol import BudgetStop, Capability, LimitStop, NodeResult, RunnerError

# Per docs/design/vendor-axis.md §2: session resume on a budget stop, structured
# output, and Bash/Read/Edit tool grants are real; 200_000 is Claude's published
# context window (Sonnet/Opus/Haiku all share it).
CAPABILITIES: Mapping[str, Any] = {
    Capability.STRUCTURED_OUTPUT.value: True,
    Capability.TOOL_USE.value: True,
    Capability.RESUME.value: True,
    Capability.STREAMING.value: True,
    Capability.MAX_CONTEXT.value: 200_000,
}

__all__ = ["DEFAULT_TIER", "TIER_EFFORT", "ClaudeCodeRunner"]

DEFAULT_TIER = "standard"

# Same mapping the API runner uses: effort belongs to the tier, not the node.
# A profile may override it under `effort:` — the vendor axis owns cost.
TIER_EFFORT = {"cheap": "low", "standard": "high", "deep": "xhigh"}

# A node is a model call, not a workstation. Measured 2026-09-02 on a login
# with the usual MCP servers and plugins configured: a trivial no-tool node
# cost ~52k input tokens with the MCP schemas loaded and ~1k without them.
# Every node in a ten-task epic was paying that before reading a line. So
# each session starts with no MCP servers and no user settings — no plugins,
# no hooks, no per-user permissions — and only the tools the profile grants.
_ISOLATION = (
    "--strict-mcp-config",
    "--mcp-config",
    '{"mcpServers":{}}',
    "--setting-sources",
    "",
)

# Tools that mutate. A role granted any of these needs edits accepted up front —
# headless mode has nobody to ask — and the profile is where that grant lives.
_WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit", "Bash"})

# Roles that return a PATCH rather than writing to a store. A build node asked
# for a unified diff with only Read/Grep/Glob is being asked to author one from
# memory — correct line numbers, correct context, and test output it had no way
# to run. Two live epics quarantined on exactly that, the handoff reporting
# "only a prose summary and a file list". So a builder gets a scratch worktree
# of its own: it edits real files, runs the project's real commands, and
# transcribes the diff git computes. The scratch is thrown away afterwards —
# the harness still applies the patch itself, in a worktree it owns.
_PATCH_ROLES = frozenset({"build"})

_DIFF_CMD = "git add -A && git diff --cached"


def _capture_diff(scratch: Path) -> str:
    """The scratch's half-written change, or "" — never a raise.

    A `BudgetStop` on a threaded role leaves the scratch behind for the next
    phase to resume into; this is what lets the stopped node hand over what it
    had already built rather than just an apology.
    """
    try:
        proc = subprocess.run(_DIFF_CMD, shell=True, capture_output=True, text=True, cwd=scratch)
    except OSError:
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def reconcile_patch(reported: str, computed: str) -> tuple[str, str | None]:
    """The scratch's own diff over the model's account of it, or a named failure.

    A reported patch beside an empty scratch means the edits landed somewhere else.
    """
    if computed:
        return computed, None
    return ("", "patch_outside_scratch") if reported.strip() else ("", "patch_empty")


def redirect_paths(text: str, repo_dir: Path, scratch: Path) -> str:
    """`text` with every path under `repo_dir` moved to the same path under `scratch`."""
    pattern = r"(?<![\w.\-])" + re.escape(str(repo_dir)) + r"(?![\w\-]|\.\w)"
    return re.sub(pattern, lambda _: str(scratch), text)


def reported_patch(stdout: str) -> str:
    """The `patch` a finished, non-error result reports, or "" — read without side effects."""
    for line in (*reversed(stdout.splitlines()), stdout):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and ("structured_output" in event or event.get("type") == "result"):
            out = event.get("structured_output")
            if event.get("is_error") or not isinstance(out, dict):
                return ""
            return str(out.get("patch") or "")
    return ""


def apply_reported_patch(scratch: Path, patch: str) -> bool:
    """Apply `patch` in `scratch` if `git apply --check` accepts it whole; never raises."""
    text = patch if patch.endswith("\n") else patch + "\n"
    try:
        for args in (["--check"], []):
            proc = subprocess.run(["git", "apply", *args], input=text, capture_output=True, text=True, cwd=scratch)
            if proc.returncode != 0:
                return False
    except OSError:
        return False
    return True


def recover_diff(scratch: Path, computed: str, stdout: str) -> tuple[str, str]:
    """The scratch diff and its source: a clean scratch falls back to an applicable reported patch."""
    if computed:
        return computed, "computed"
    reported = reported_patch(stdout)
    if reported.strip() and apply_reported_patch(scratch, reported):
        recomputed = _capture_diff(scratch)
        if recomputed:
            return recomputed, "reported"
    return computed, "computed"


def _unprefixed(token: str) -> str:
    """Strip the one-letter path prefix git puts on a `+++`/`---` token — `a/`, `b/`, or (under `diff.mnemonicPrefix`) `c/`, `i/`, `w/`, `o/`."""
    _, sep, rest = token.partition("/")
    return rest if sep else token


def files_touched_from_patch(patch: str) -> list[str]:
    """The paths a patch changes, read from its own '+++'/'---' headers — never asked.

    `+++ b/<path>` names every add or modify; a deletion has `+++ /dev/null` and
    is named by `--- a/<path>` instead. Pure: no git invoked, no filesystem touched.
    """
    seen: list[str] = []
    minus_token = ""
    for line in patch.splitlines():
        if line.startswith("--- "):
            minus_token = line[4:]
        elif line.startswith("+++ "):
            plus_token = line[4:]
            path = _unprefixed(minus_token if plus_token == "/dev/null" else plus_token)
            if path and path not in seen:
                seen.append(path)
    return seen


# Errors that are about the CALL, not about the work. The provider's own
# safeguard classifier occasionally flags an ordinary node message — an
# arbitration prompt quoting two reviewers reads, to a classifier, like an
# argument — and the CLI returns an error with nothing wrong on this side of
# the boundary. It hit `arbitrate` in runs 9 and 16, and each time quarantined
# a task whose builds and reviews were already complete, at roughly $4 a time.
#
# Matched on the CLI's own words rather than on an exit code, because the exit
# code is the same one a real refusal returns.
_TRANSIENT_ERRORS = ("safeguards flagged", "reasoning_extraction", "error_max_structured_output_retries")

# A `subtype: success` payload whose result text is this banner is the account's
# own session limit, not a node failure.
_LIMIT_BANNER_RE = re.compile(r"you've hit your session limit|usage limit", re.IGNORECASE)


def _is_transient(payload: Mapping[str, Any]) -> bool:
    """Pure: is this error about the call rather than about the node's work?

    Everything the CLI said, lowercased and searched. A false negative costs
    what it already costs today; a false positive costs one repeated node, once,
    which is why the retry below is capped at one and not made a loop.
    """
    said = " ".join(
        str(payload.get(key) or "") for key in ("subtype", "result", "errors")
    ).lower()
    return any(marker in said for marker in _TRANSIENT_ERRORS)


def is_safeguard_refusal(payload: Mapping[str, Any]) -> bool:
    """Pure: is this specifically the provider's safeguard classifier?

    Narrower than `_is_transient` — scoped to the one phrase the CLI uses for
    it, so only this failure earns an alternate-model retry and its own
    ledger reason rather than a plain same-model one.
    """
    said = " ".join(
        str(payload.get(key) or "") for key in ("subtype", "result", "errors")
    ).lower()
    return "safeguards flagged" in said


def is_max_structured_output_retries(payload: Mapping[str, Any]) -> bool:
    """Pure: did the CLI give up parsing structured output after its own retries?

    Narrower than `_is_transient` for the same reason as `is_safeguard_refusal`:
    scoped to the one subtype the CLI uses, so only this failure earns the
    alternate-model retry and its own ledger reason.
    """
    said = " ".join(
        str(payload.get(key) or "") for key in ("subtype", "result", "errors")
    ).lower()
    return "error_max_structured_output_retries" in said


def _alt_model_for(tiers: Mapping[str, Any], tier: str, model: str) -> str:
    """The tier's next bound model after `model`, wrapping; `model` itself if the tier names only one."""
    bindings = tiers.get(tier)
    if not isinstance(bindings, list) or len(bindings) < 2:
        return model
    names = [str(b) for b in bindings]
    idx = names.index(model) if model in names else -1
    return names[(idx + 1) % len(names)]


def next_spent(previous: float, reported_usd: float, stopped: bool) -> float:
    """A stop's `total_cost_usd` is the session's, replacing; a success's is this call's, adding."""
    return reported_usd if stopped else previous + reported_usd


def _tool_result_text(content: Any) -> str:
    """A tool result's own text, however the CLI shaped it."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(block.get("text") or "") for block in content if isinstance(block, Mapping) and block.get("type") == "text"
        )
    return ""


def trace_commands(trace: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    """Every Bash call the trace shows was run, paired with its own tool result, in order.

    Matched by `tool_use_id`, since parallel tool calls in one assistant turn
    do not resolve in the order they were issued. Pure: reads `trace`, never
    mutates it, touches nothing on disk.
    """
    pending: dict[str, str] = {}
    commands: list[dict[str, str]] = []
    for event in trace:
        if not isinstance(event, Mapping):
            continue
        message = event.get("message")
        content = message.get("content") if isinstance(message, Mapping) else None
        if event.get("type") == "assistant":
            for block in content or []:
                if isinstance(block, Mapping) and block.get("type") == "tool_use" and block.get("name") == "Bash":
                    tool_input = block.get("input")
                    command = tool_input.get("command") if isinstance(tool_input, Mapping) else None
                    tool_id = block.get("id")
                    if command and tool_id:
                        pending[tool_id] = str(command)
        elif event.get("type") == "user":
            for block in content or []:
                if isinstance(block, Mapping) and block.get("type") == "tool_result" and block.get("tool_use_id") in pending:
                    commands.append({
                        "command": pending.pop(block["tool_use_id"]),
                        "output": _tool_result_text(block.get("content")),
                        "source": "trace",
                    })
    return commands


def self_reported_commands(data: Mapping[str, Any]) -> list[dict[str, str]]:
    """The model's own account of what it ran — kept only when no trace exists to derive it."""
    reported = data.get("commands_run")
    if not isinstance(reported, list):
        return []
    return [{"command": str(item), "source": "self_report"} for item in reported if item]


def _call_fields(
    role: str,
    tier: str,
    model: str,
    tools: Sequence[str],
    payload: Mapping[str, Any],
    task_id: str | None = None,
    *,
    ceiling_usd: float | None = None,
    ceiling_source: str = "profile",
) -> dict[str, Any]:
    """The one shape a call is recorded in — success or failure alike."""
    usage = payload.get("usage") if isinstance(payload.get("usage"), Mapping) else {}
    return {
        "role": role,
        "task_id": task_id,
        "tier": tier,
        "model": model,
        "tools": list(tools),
        "cost_usd": payload.get("total_cost_usd"),
        "ceiling_usd": ceiling_usd,
        "ceiling_source": ceiling_source,
        "turns": payload.get("num_turns"),
        "duration_ms": payload.get("duration_ms"),
        # Split, not summed: a cache read costs a tenth of a fresh token,
        # and "4.6M input" meant nothing until the price revealed that most
        # of it was cached. Now the record says so itself.
        "input_tokens": int(usage.get("input_tokens") or 0),
        "cache_read_tokens": int(usage.get("cache_read_input_tokens") or 0),
        "cache_creation_tokens": int(usage.get("cache_creation_input_tokens") or 0),
        "input_total": sum(
            int(usage.get(k) or 0)
            for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
        ),
        "output_tokens": int(usage.get("output_tokens") or 0),
        **({"trace": payload["trace"]} if payload.get("trace") else {}),
        **({"commands_run": payload["commands_run"]} if "commands_run" in payload else {}),
    }


def _load_bounds(path: Path) -> dict[tuple[str, str], Mapping[str, Any]] | None:
    """Rows from `cox stats bounds --write`, keyed by (role, model). None when unreadable."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"warning: cost bounds file unreadable ({path}): {exc}", file=sys.stderr)
        return None
    rows = raw.get("rows") if isinstance(raw, Mapping) else None
    if not isinstance(rows, list):
        return {}
    return {(str(r.get("role")), str(r.get("model"))): r for r in rows if isinstance(r, Mapping)}


def _effective_limit(shape_ceiling: float | None, node_cap: float | None) -> tuple[float | None, bool]:
    """min(shape, cap) per cost-bounds.md §6 rule 2. True when the cap is the binding number."""
    if node_cap is None:
        return shape_ceiling, False
    if shape_ceiling is None or node_cap < shape_ceiling:
        return node_cap, True
    return shape_ceiling, False


class ClaudeCodeRunner:
    """Runs nodes as headless Claude Code sessions with structured output."""

    def __init__(
        self,
        profile: Mapping[str, Any],
        *,
        role_skills: Mapping[str, str] | None = None,
        cwd: Path | str | None = None,
        repo_dir: Path | str | None = None,
        claude_bin: str | None = None,
        timeout: int = 1800,
        extra_system: str = "",
        trace_dir: Path | str | None = None,
        runs_dir: Path | str | None = None,
        run_id: str | None = None,
    ) -> None:
        self.profile = dict(profile)
        self.capabilities = dict(CAPABILITIES)
        self.tiers = dict(self.profile.get("tiers") or {})
        if not self.tiers:
            raise RunnerError("provider profile declares no tiers")
        raw_tools = self.profile.get("tools") or {}
        if not isinstance(raw_tools, Mapping):
            raise RunnerError("provider profile 'tools' must map role -> list of tool names")
        self.tools = {str(role): [str(t) for t in (names or [])] for role, names in raw_tools.items()}
        raw_effort = self.profile.get("effort") or {}
        if not isinstance(raw_effort, Mapping):
            raise RunnerError("provider profile 'effort' must map tier -> effort level")
        self.effort = {**TIER_EFFORT, **{str(k): str(v) for k, v in raw_effort.items()}}
        # Cost ceilings, in dollars, per tier and optionally per role. The CLI
        # stops the session when it is reached and says so (`error_max_budget_usd`),
        # which the harness records as a quarantine — bounded and visible beats
        # a 101-turn build that finished anyway.
        self.budget_usd = {str(k): float(v) for k, v in (self.profile.get("budget_usd") or {}).items()}
        self.role_budget_usd = {str(k): float(v) for k, v in (self.profile.get("role_budget_usd") or {}).items()}
        # The operator's per-node spend cap (docs/design/cost-bounds.md §1), set
        # by the harness after construction, like `runs_dir`/`run_id`. Unset
        # means the shape ceiling above is the only limit, exactly as before.
        self.node_cap_usd: float | None = None
        # The bounds file `cox stats bounds --write` produces, and the level
        # column to read from it (docs/design/cost-bounds.md §2/§3/§6). Set by
        # the harness after construction, like `node_cap_usd`; unset means the
        # shape ceiling above is the only one, exactly as before.
        self.cost_bounds_path: Path | None = None
        self.cost_level: str = "moderate"
        self._bounds: dict[tuple[str, str], Mapping[str, Any]] | None = None
        self._bounds_loaded = False
        # A profile may reassign a role's tier — the vendor axis owning cost.
        # Extraction-shaped roles a graph asked "standard" for can run cheap here.
        self.tier_overrides = {str(k): str(v) for k, v in (self.profile.get("tier_overrides") or {}).items()}
        # A tool-computed map of the target repository, set by the harness. Shown
        # to roles that have tools, so they read it instead of drawing their own.
        self.repo_digest: str | None = None
        # Where to keep a turn-by-turn trace of every node, if anywhere. Set from
        # AGENT_GRAPHS_TRACE_DIR or by the harness. Without it a 44-turn build
        # is a number; with it, it is a list of what each turn did.
        self.trace_dir: Path | None = Path(trace_dir).expanduser() if trace_dir else None
        # Where the per-call ledger lives: `<runs_dir>/<run_id>.calls.jsonl`, one
        # line per call, appended as it returns — success or `RunnerError` alike.
        # Learned the way trace_dir is, so a run that dies mid-node still leaves
        # a record of what it spent instead of only what a survivor remembers.
        self.runs_dir: Path | None = Path(runs_dir).expanduser() if runs_dir else None
        self.run_id: str | None = run_id
        # The project's own check commands, verbatim from the cartridge, set by
        # the harness. Traced builds spent a third of their turns discovering
        # how to run the tests — the wrong interpreter, `which pytest`,
        # `--version`, `echo hello`. The harness knows; the builder is told.
        self.check_commands: list[str] = []
        # Threads: one Claude Code session and one scratch tree per continuity
        # hint, so plan, build and a retry run on the same instance and the
        # retry edits a tree it already edited. Closed by the harness when the
        # phase is done; never shared across a review boundary, because the
        # graph never hands review the hint.
        self._threads: dict[str, dict[str, Any]] = {}
        self.role_skills = dict(role_skills or {})
        self.cwd = Path(cwd).expanduser().resolve() if cwd else Path.cwd()
        self.repo_dir: Path | None = Path(repo_dir).expanduser().resolve() if repo_dir else None
        self.claude_bin = claude_bin or str(self.profile.get("command") or "claude")
        self.timeout = timeout
        self.extra_system = extra_system
        # One row per node: what it cost and how many turns it took. Read by
        # whoever wants to know what a run spent; never by a graph.
        self.calls: list[dict[str, Any]] = []

    def _append_call_ledger(self, call: Mapping[str, Any], *, ok: bool, error: str | None = None) -> None:
        """One JSON line per call, written as it returns — never a rewrite, never buffered."""
        if not self.runs_dir or not self.run_id:
            return
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        row = {**call, "ts": datetime.now(UTC).isoformat(), "ok": ok}
        if error is not None:
            row["error"] = error
        path = self.runs_dir / f"{self.run_id}.calls.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")

    # ── resolution ──────────────────────────────────────────────────────────

    def _model_for(self, tier: str) -> str:
        model = self.tiers.get(tier)
        if not model:
            known = ", ".join(sorted(self.tiers))
            raise RunnerError(f"provider profile has no model for tier '{tier}'; it declares: {known}")
        return str(model[0]) if isinstance(model, list) else str(model)

    def _bounds_row(self, role: str | None, model: str) -> Mapping[str, Any] | None:
        """The bounds file's row for (role, model), loaded and cached once."""
        if not self._bounds_loaded:
            self._bounds = _load_bounds(Path(self.cost_bounds_path)) if self.cost_bounds_path else {}
            self._bounds_loaded = True
        return (self._bounds or {}).get((role, model))

    def _shape_ceiling(self, role: str | None, tier: str, model: str, budget_usd: float | None) -> tuple[float | None, str]:
        """The provider's own ceiling for this call, before any operator cap applies.

        docs/design/cost-bounds.md §6 rule 6: the bounds row wins over the
        profile only when it has `n >= 20` and no explicit override was given.
        """
        if budget_usd is None:
            row = self._bounds_row(role, model)
            if row is not None and (row.get("n") or 0) >= 20 and row.get(self.cost_level) is not None:
                return float(row[self.cost_level]), f"bounds:{self.cost_level}"
        budget = budget_usd
        if budget is None:
            budget = self.role_budget_usd.get(role) if role is not None else None
        if budget is None:
            budget = self.budget_usd.get(tier)
        return budget, "profile"

    @staticmethod
    def _read_context(context: Sequence[str]) -> str:
        """Context packs are read HERE, at the edge — never inside a graph."""
        chunks = []
        for entry in context:
            path = Path(entry)
            try:
                chunks.append(f"<context path=\"{path.name}\">\n{path.read_text(encoding='utf-8')}\n</context>")
            except OSError as exc:
                raise RunnerError(f"cannot read context pack {path}: {exc}") from exc
        return "\n\n".join(chunks)

    def _make_scratch(self, role: str) -> tuple[Path, Path]:
        parent = Path(tempfile.mkdtemp(prefix="agent-graphs-build-"))
        scratch = parent / "tree"
        proc = subprocess.run(
            ["git", "-C", str(self.repo_dir), "worktree", "add", "--detach", str(scratch), "HEAD"],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            shutil.rmtree(parent, ignore_errors=True)
            raise RunnerError(f"could not create a scratch worktree for '{role}': {(proc.stderr or '').strip()[:300]}")
        return parent, scratch

    def _drop_scratch(self, parent: Path, scratch: Path) -> None:
        subprocess.run(
            ["git", "-C", str(self.repo_dir), "worktree", "remove", "--force", str(scratch)],
            capture_output=True, text=True,
        )
        shutil.rmtree(parent, ignore_errors=True)

    def _thread(self, name: str, role: str) -> dict[str, Any]:
        """The thread's state, created on first use: a session id and, given a repository, a scratch."""
        state = self._threads.get(name)
        if state is None:
            state = {"session": str(uuid.uuid4()), "parent": None, "scratch": None, "calls": 0}
            if self.repo_dir:
                state["parent"], state["scratch"] = self._make_scratch(role)
            self._threads[name] = state
        return state

    def close_thread(self, name: str) -> None:
        state = self._threads.pop(name, None)
        if state and state.get("scratch") is not None:
            self._drop_scratch(state["parent"], state["scratch"])

    def close(self) -> None:
        """Drop every thread's scratch. The harness calls this when a phase is done."""
        for name in list(self._threads):
            self.close_thread(name)

    @contextmanager
    def _scratch(self, role: str) -> Iterator[Path | None]:
        """A disposable worktree for a patch-returning role, or nothing.

        `git worktree add --detach` off the repository the run targets, so the
        builder edits a real tree at the right commit and `git diff` computes
        the patch instead of the model recalling one. Parallel builds in one
        phase each get their own, which is also why this cannot be the phase
        worktree they share. Removed on the way out, success or not.
        """
        if role not in _PATCH_ROLES or not self.repo_dir:
            yield None
            return
        parent, scratch = self._make_scratch(role)
        try:
            yield scratch
        finally:
            self._drop_scratch(parent, scratch)

    def _allowed_bash(self) -> list[str]:
        """The Bash prefixes this session permits, in `--allowedTools` form.

        One source for two consumers: `_argv` enforces this list and
        `_workspace` tells the node what is on it. Computing it twice is how a
        builder ends up discovering the boundary by hitting it.
        """
        allowed = [f"Bash({cmd.split()[0]}:*)" for cmd in self.check_commands if cmd.split()]
        allowed += ["Bash(git status:*)", "Bash(git diff:*)", "Bash(git add:*)"]
        return list(dict.fromkeys(allowed))

    def _workspace(self, scratch: Path | None = None, *, patches: bool = True) -> str:
        """Tell the node where the world is. It cannot find out on its own."""
        lines = [
            "<workspace>",
            f"Your working directory is {self.cwd}. It is the work store root: `work/` under it "
            "holds initiatives as work/<initiative>/<phase>/<task>.md.",
        ]
        if self.repo_digest and (scratch is not None or self.repo_dir):
            lines.append(
                "A map of the target repository, computed by tools, follows. Consult it FIRST: it "
                "tells you which files exist, how long they are, and which functions and classes "
                "live at which line. Open only the files you actually need, read a file once, and "
                "never list or grep the tree to learn what this map already says.\n"
                f"<repo-digest>\n{self.repo_digest}\n</repo-digest>"
            )
        if scratch is not None and not patches:
            lines.append(
                f"You have your own checkout of the target repository at {scratch}, at the commit "
                "this run builds on. Read it there. A later step on this same thread will edit it "
                "and produce the patch; you do not."
            )
        if scratch is not None and patches:
            lines.append(
                f"You have a scratch checkout of the target repository at {scratch}, at the commit "
                "this run builds on, and it is YOURS — nobody else is working in it and it is "
                "deleted when you return. Make your changes there as real edits, run the "
                "project's own test command there, and then produce the patch by running "
                f"`{_DIFF_CMD}` in it and returning that output VERBATIM as your `patch` field. "
                "Do not hand-write a diff, do not reformat what git printed, and do not commit. "
                "Report the commands you actually ran and their real output; a command you did "
                "not run is not evidence. The harness applies your patch itself, in a different "
                "worktree, so leaving the scratch dirty is expected and correct."
            )
            if self.check_commands:
                cmds = "; ".join(self.check_commands)
                lines.append(
                    f"The project's checks are exactly: `{cmds}`. Run them as written, from the scratch "
                    "root, and nothing else to test with: the environment is already set up, the "
                    "right interpreter and packages are on PATH for those commands, and probing for "
                    "them (`which`, `--version`, `python -m ...`, `echo`) is a wasted turn every time. "
                    "If a command as written fails to start, report that verbatim and stop. Run every "
                    "one of them before you produce the diff, a lint command as much as the tests: a "
                    "check you skip here fails after review and costs a whole rerun."
                )
            permitted = ", ".join(
                f"`{name[len('Bash('):-len(':*)')]}`" for name in self._allowed_bash()
            )
            lines.append(
                f"The ONLY shell commands permitted in this session are: {permitted}. Anything "
                "else is refused by the sandbox before it runs. If your task text asks you to "
                "run a command that is not on that list, do not attempt it and do not record "
                "the refusal in `commands_run` as though it were output: a refusal is not "
                "evidence, and downstream it reads as a builder that did not do its job rather "
                "than as a sandbox that said no. Say plainly in your summary that the command "
                "is not permitted in this session and that the harness must arrange for it. "
                "Substituting your own reading of the diff for a command you could not run is "
                "the same mistake in the other direction — that is a recollection, not a check."
            )
            lines.append(
                "Work in as few turns as you can. Every turn re-sends everything you have read, so "
                "the cost of a session grows with the square of its length: read the map, open the "
                "few files you must, make the edits, run the test command at most twice, produce the "
                "diff, return. Do not re-read a file, do not explore for context you were already "
                "given, and do not polish. A session that exceeds its budget is stopped and the task "
                "is quarantined, so a finished-but-plain patch beats an unfinished perfect one."
            )
        if self.repo_dir or scratch is not None:
            where = scratch if scratch is not None else self.repo_dir
            lines.append(
                f"Always use ABSOLUTE paths under {where} — a relative path resolves against a "
                "directory that is not the repository, and every failed read is a wasted turn. "
                "Read by range: the map gives line numbers, so open the 40-80 lines around the "
                "symbol you need (Read's offset and limit), and never read a file longer than 300 "
                "lines whole. Each turn re-sends everything already read, so a whole-file read of "
                "a large module taxes every turn that follows it."
            )
        if self.repo_dir and scratch is not None and patches:
            lines.append(
                f"The scratch at {scratch} is the only copy of the repository you may read or edit. "
                "Your diff is relative to its root, with a/ and b/ prefixes. Any other absolute path "
                "to this repository that you meet in the task text is the same path inside the scratch."
            )
        elif self.repo_dir:
            lines.append(
                f"The repository this run targets is checked out at {self.repo_dir}. Read it there. "
                "Any unified diff you return uses paths relative to that repository's root "
                "(with a/ and b/ prefixes) and is applied by the harness, never by you. "
                "Read it there for context; it is shared, so never edit it. "
                "Patches returned by earlier nodes in this run are NOT applied in that checkout: "
                "the harness applies them later, in a worktree of its own. Judge a patch from its "
                "text, never from whether the checkout already contains it."
            )
        lines.append(
            "You have exactly the tools listed for this session and no others. If you have "
            "none, answer from the prompt alone."
        )
        lines.append("</workspace>")
        return "\n".join(lines)

    def _argv(self, *, model: str, tier: str, tools: Sequence[str], schema: Mapping[str, Any], system: str, scratch: Path | None = None, role: str | None = None, session: Sequence[str] = (), budget_usd: float | None = None, spent_usd: float = 0.0) -> list[str]:
        argv = [
            self.claude_bin,
            "-p",
            *(session or ["--no-session-persistence"]),
            "--output-format",
            "stream-json" if self.trace_dir else "json",
            *(["--verbose"] if self.trace_dir else []),
            "--model",
            model,
            "--effort",
            self.effort.get(tier, "high"),
            "--json-schema",
            json.dumps(dict(schema)),
            *_ISOLATION,
        ]
        ceiling, _ = self._shape_ceiling(role, tier, model, budget_usd)
        effective, _ = _effective_limit(ceiling, self.node_cap_usd)
        if effective is not None:
            # Resuming a stopped session may see the ceiling as covering the
            # whole session's spend rather than this invocation's, so the
            # fresh slice must cover at least the ceiling either way.
            argv += ["--max-budget-usd", f"{spent_usd + effective:.4f}"]
        if system:
            argv += ["--system-prompt", system]
        # A builder's scratch is the only repository tree it may touch. Granting
        # the phase worktree beside it let a builder edit there, where its
        # tests were refused and its scratch diff came back empty.
        sealed = role in _PATCH_ROLES and scratch is not None
        for extra in (None if sealed else self.repo_dir, scratch):
            if extra is not None and Path(extra) != self.cwd:
                argv += ["--add-dir", str(extra)]
        if _WRITE_TOOLS & set(tools):
            argv += ["--permission-mode", "acceptEdits"]
        # acceptEdits covers Write and Edit and nothing else: every Bash call a
        # builder made in the first seven epics came back "requires approval",
        # so no test ever ran and the builder probed the environment instead.
        # Bash is pre-approved for exactly the project's checks and the git
        # verbs the diff needs — prefixes, so `pytest tests/x.py -q` passes —
        # and denied for everything else, which is what a scratch tree wants.
        if "Bash" in tools:
            argv += ["--allowedTools", *self._allowed_bash()]
        # Last on purpose: `--tools` is variadic, and nothing may follow it that
        # could be mistaken for a tool name. The prompt travels on stdin.
        argv += ["--tools", *(tools or [""])]
        return argv

    def _payload(self, role: str, stdout: str) -> dict[str, Any]:
        """The result object — the whole output in json mode, the last event in stream mode.

        In stream mode every event is also written to the trace file, one per
        line, so the record of WHAT a node did survives the node.
        """
        if not self.trace_dir:
            try:
                payload = json.loads(stdout)
            except json.JSONDecodeError as exc:
                raise RunnerError(f"node '{role}': claude output is not JSON: {stdout[:200]}") from exc
            return payload if isinstance(payload, dict) else {"is_error": True, "result": f"non-object output: {stdout[:200]}"}

        self.trace_dir.mkdir(parents=True, exist_ok=True)
        n = sum(1 for c in self.calls if c["role"] == role) + 1
        path = self.trace_dir / f"{role}-{n}.jsonl"
        path.write_text(stdout + "\n", encoding="utf-8")
        events: list[dict[str, Any]] = []
        last: dict[str, Any] | None = None
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            events.append(event)
            if event.get("type") == "result":
                last = event
        if last is None:
            raise RunnerError(f"node '{role}': no result event in the stream (trace at {path})")
        last["trace"] = str(path)
        last["commands_run"] = trace_commands(events)
        return last

    # ── execution ───────────────────────────────────────────────────────────

    def _invoke(
        self, *, role: str, tier: str, model: str, tools: Sequence[str], schema: Mapping[str, Any], prompt: str,
        packs: Sequence[str], scratch: Path | None, patches: bool, session: Sequence[str], budget_usd: float | None = None,
        spent_usd: float = 0.0,
    ) -> subprocess.CompletedProcess[str]:
        system = "\n\n".join(
            part for part in (self._read_context(packs), self._workspace(scratch, patches=patches), self.extra_system) if part
        )
        if role in _PATCH_ROLES and scratch is not None and self.repo_dir:
            # The plan was written in the phase worktree and may cite its paths;
            # a builder that follows one leaves its scratch clean.
            system, prompt = (redirect_paths(text, self.repo_dir, scratch) for text in (system, prompt))
        argv = self._argv(
            model=model, tier=tier, tools=tools, schema=schema, system=system, scratch=scratch, role=role,
            session=session, budget_usd=budget_usd, spent_usd=spent_usd,
        )
        try:
            return subprocess.run(
                argv,
                input=prompt,
                capture_output=True,
                text=True,
                # A thread keeps one working directory for its whole life: the
                # CLI files sessions by directory, and a resume looks there.
                cwd=scratch or self.cwd,
                timeout=self.timeout,
            )
        except FileNotFoundError as exc:
            raise RunnerError(f"'{self.claude_bin}' not found; is Claude Code installed and on PATH?") from exc
        except subprocess.TimeoutExpired as exc:
            raise RunnerError(f"node '{role}' did not finish within {self.timeout}s") from exc

    def run(
        self,
        *,
        role: str,
        tier: str = DEFAULT_TIER,
        schema: Mapping[str, Any],
        prompt: str,
        context: Sequence[str] = (),
        thread: str | None = None,
        budget_usd: float | None = None,
        task: str | None = None,
    ) -> NodeResult:
        tier = self.tier_overrides.get(role, tier)
        model = self._model_for(tier)
        body = self.role_skills.get(role)
        packs = [body, *context] if body else list(context)
        tools = self.tools.get(role, [])

        # Two attempts at most, and the second only for an error that is about
        # the call rather than about the work. A retry loop on a model error is
        # how a budget disappears; one retry is how a transient classifier
        # misfire stops costing a finished task its run.
        computed_patch, has_scratch, patch_source = "", False, "computed"
        attempt_model = model
        first_call_id = str(uuid.uuid4())
        retry_extra: dict[str, Any] = {}
        for attempt in (1, 2):
            call_id = first_call_id if attempt == 1 else str(uuid.uuid4())
            used_model = attempt_model
            if thread:
                state = self._thread(thread, role)
                session = ["--session-id", state["session"]] if state["calls"] == 0 else ["--resume", state["session"]]
                proc = self._invoke(
                    role=role, tier=tier, model=used_model, tools=tools, schema=schema, prompt=prompt, packs=packs,
                    scratch=state["scratch"], patches=role in _PATCH_ROLES, session=session, budget_usd=budget_usd,
                    spent_usd=state.get("spent_usd", 0.0),
                )
                if role in _PATCH_ROLES and state.get("scratch"):
                    has_scratch = True
                    computed_patch, patch_source = recover_diff(
                        state["scratch"], _capture_diff(state["scratch"]), (proc.stdout or "").strip()
                    )
            else:
                with self._scratch(role) as scratch:
                    proc = self._invoke(
                        role=role, tier=tier, model=used_model, tools=tools, schema=schema, prompt=prompt, packs=packs,
                        scratch=scratch, patches=True, session=(), budget_usd=budget_usd,
                    )
                    if role in _PATCH_ROLES and scratch:
                        has_scratch = True
                        computed_patch, patch_source = recover_diff(
                            scratch, _capture_diff(scratch), (proc.stdout or "").strip()
                        )

            stdout = (proc.stdout or "").strip()
            if not stdout:
                tail = (proc.stderr or "").strip()[-800:]
                raise RunnerError(f"node '{role}': claude exited {proc.returncode} with no output: {tail}")
            payload = self._payload(role, stdout)
            if not isinstance(payload, dict):
                raise RunnerError(f"node '{role}': claude output is {type(payload).__name__}, expected an object")
            if not payload.get("is_error"):
                if thread:
                    # Only a call that did not fail advances the thread's
                    # counter. A transient failure on the first call left no
                    # session behind for a `--resume` to find, so the retry
                    # must repeat the exact flags the failed attempt used.
                    state["calls"] += 1
                    # A success's `total_cost_usd` is this call's own spend, not the
                    # session's: three resumed builds on one ticket reported 1.56, 0.67,
                    # 0.86 — falling then rising, which a cumulative figure cannot do.
                    state["spent_usd"] = next_spent(
                        state.get("spent_usd", 0.0), float(payload.get("total_cost_usd") or 0.0), stopped=False
                    )
                break

            # Name everything the CLI said about it. A bare `None` result was
            # the whole diagnosis of a build failure once; never again.
            detail = {k: payload.get(k) for k in ("subtype", "result", "errors", "num_turns", "duration_ms") if payload.get(k) is not None}
            shape_ceiling, ceiling_source = self._shape_ceiling(role, tier, used_model, budget_usd)
            _, cap_governs = _effective_limit(shape_ceiling, self.node_cap_usd)
            cap_stop = cap_governs and detail.get("subtype") == "error_max_budget_usd"
            if cap_stop:
                detail["subtype"] = "error_spend_cap"
            message = f"node '{role}' failed in claude: {json.dumps(detail)[:800]}"
            if cap_stop:
                ceiling_txt = f"${shape_ceiling:.4f}" if shape_ceiling is not None else "none"
                message += f" (cap ${self.node_cap_usd:.4f} < shape ceiling {ceiling_txt})"
            if attempt == 2 or not _is_transient(payload):
                # Every failed attempt that ends the node is billed, whether it
                # stops the budget or raises outright — ledgered here, once,
                # before either exit, with the trace path the CLI just reported.
                self._append_call_ledger(
                    {
                        **_call_fields(
                            role, tier, used_model, tools, payload, task,
                            ceiling_usd=shape_ceiling, ceiling_source=ceiling_source,
                        ),
                        "id": call_id, **retry_extra,
                    },
                    ok=False, error=message,
                )
                if payload.get("subtype") == "error_max_budget_usd":
                    spent = float(payload.get("total_cost_usd") or 0.0)
                    partial_patch = ""
                    if thread:
                        # Leave `state` exactly as it was — same scratch, same
                        # `calls` — so a later `run(..., thread=same)` resumes
                        # this session instead of starting the node over.
                        state["spent_usd"] = next_spent(state.get("spent_usd", 0.0), spent, stopped=True)
                        if state.get("scratch"):
                            partial_patch = _capture_diff(state["scratch"])
                    raise BudgetStop(
                        role=role,
                        thread=thread,
                        session=state["session"] if thread else None,
                        spent_usd=spent,
                        detail=message,
                        partial_patch=partial_patch,
                    )
                raise RunnerError(message)

            # A safeguard refusal, or the CLI's own structured-output retries
            # running out, moves the retry to the tier's alternate model and
            # names why on the ledger; a plain reasoning-extraction misfire
            # keeps retrying the same model exactly as before.
            if is_safeguard_refusal(payload):
                attempt_model = _alt_model_for(self.tiers, tier, used_model)
                retry_extra = {"retry_of": call_id, "reason": "safeguard_refusal"}
            elif is_max_structured_output_retries(payload):
                attempt_model = _alt_model_for(self.tiers, tier, used_model)
                retry_extra = {"retry_of": call_id, "reason": "structured_output"}

            # Keep the failed attempt's trace. The retry writes to the same
            # filename, and a transient error that leaves no record behind is
            # one nobody can measure the frequency of later. Renamed BEFORE the
            # ledger line is written, so the failed row names the file that
            # still holds its trace rather than the one the retry is about to
            # reuse — the ledger and the trace file agree on one path each.
            traced_payload = payload
            if payload.get("trace"):
                failed = Path(payload["trace"]).replace(Path(payload["trace"]).with_suffix(".error.jsonl"))
                traced_payload = {**payload, "trace": str(failed)}
            self._append_call_ledger(
                {
                    **_call_fields(
                        role, tier, used_model, tools, traced_payload, task,
                        ceiling_usd=shape_ceiling, ceiling_source=ceiling_source,
                    ),
                    "id": call_id,
                },
                ok=False, error=message,
            )

        # Built before either raise below, so a malformed answer is ledgered
        # too — the run spent the call whether or not it parsed.
        ceiling_usd, ceiling_source = self._shape_ceiling(role, tier, used_model, budget_usd)
        call = {
            **_call_fields(
                role, tier, used_model, tools, payload, task,
                ceiling_usd=ceiling_usd, ceiling_source=ceiling_source,
            ),
            "id": call_id, **retry_extra,
        }
        # The account's session limit arrives as a successful call whose whole
        # text is the banner. It is ledgered like every call that ends a node
        # — the invariant at the BudgetStop path above — and then pauses the
        # run instead of quarantining the task (arbiter, graphs-limit-pause-3).
        if _LIMIT_BANNER_RE.search(str(payload.get("result") or "")):
            self._append_call_ledger(call, ok=False, error="account session limit")
            raise LimitStop(detail=str(payload["result"]))
        data = payload.get("structured_output")
        if data is None:
            # An older build, or a session that answered in prose: the result
            # text is the last resort, and it has to parse or the node failed.
            try:
                data = json.loads(str(payload.get("result") or ""))
            except json.JSONDecodeError as exc:
                message = f"node '{role}' returned no structured output and its text is not JSON: {exc}"
                self._append_call_ledger(call, ok=False, error=message)
                raise RunnerError(message) from exc
        if not isinstance(data, dict):
            message = f"node '{role}' returned {type(data).__name__}, expected an object"
            self._append_call_ledger(call, ok=False, error=message)
            raise RunnerError(message)

        # Appended now, not after reconciliation below: a same-role retry's
        # trace file is numbered from this count (`_payload`), and a call that
        # fails the patch_empty gate must still hold its slot or the retry
        # overwrites the failed attempt's own trace file.
        self.calls.append(call)
        if role in _PATCH_ROLES and has_scratch:
            patch, reason = reconcile_patch(str(data.get("patch") or ""), computed_patch)
            if reason:
                what = (
                    "the reported patch is not in the scratch tree and does not apply to it"
                    if reason == "patch_outside_scratch"
                    else "the scratch tree has no changes to show for it"
                )
                message = f"node '{role}' {reason}: {what}"
                self._append_call_ledger(call, ok=False, error=message)
                raise RunnerError(message)
            data = {**data, "patch": patch}
        self.calls[-1] = {
            **call,
            **({"patch_source": patch_source} if has_scratch and role in _PATCH_ROLES else {}),
            "files_touched": files_touched_from_patch(str(data.get("patch") or "")),
            "commands_run": call.get("commands_run", self_reported_commands(data)),
        }
        self._append_call_ledger(self.calls[-1], ok=True)
        return NodeResult(data)
