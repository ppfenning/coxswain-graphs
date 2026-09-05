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
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from runner.protocol import BudgetStop, NodeResult, RunnerError

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

# Errors that are about the CALL, not about the work. The provider's own
# safeguard classifier occasionally flags an ordinary node message — an
# arbitration prompt quoting two reviewers reads, to a classifier, like an
# argument — and the CLI returns an error with nothing wrong on this side of
# the boundary. It hit `arbitrate` in runs 9 and 16, and each time quarantined
# a task whose builds and reviews were already complete, at roughly $4 a time.
#
# Matched on the CLI's own words rather than on an exit code, because the exit
# code is the same one a real refusal returns.
_TRANSIENT_ERRORS = ("safeguards flagged", "reasoning_extraction")


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
    ) -> None:
        self.profile = dict(profile)
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

    # ── resolution ──────────────────────────────────────────────────────────

    def _model_for(self, tier: str) -> str:
        model = self.tiers.get(tier)
        if not model:
            known = ", ".join(sorted(self.tiers))
            raise RunnerError(f"provider profile has no model for tier '{tier}'; it declares: {known}")
        return str(model)

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
                    "If a command as written fails to start, report that verbatim and stop."
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
        if self.repo_dir:
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
        budget = budget_usd
        if budget is None:
            budget = self.role_budget_usd.get(role) if role is not None else None
        if budget is None:
            budget = self.budget_usd.get(tier)
        if budget is not None:
            # Resuming a stopped session may see the ceiling as covering the
            # whole session's spend rather than this invocation's, so the
            # fresh slice must cover at least the ceiling either way.
            argv += ["--max-budget-usd", f"{spent_usd + budget:.4f}"]
        if system:
            argv += ["--system-prompt", system]
        for extra in (self.repo_dir, scratch):
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
        last: dict[str, Any] | None = None
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and event.get("type") == "result":
                last = event
        if last is None:
            raise RunnerError(f"node '{role}': no result event in the stream (trace at {path})")
        last["trace"] = str(path)
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
        for attempt in (1, 2):
            if thread:
                state = self._thread(thread, role)
                session = ["--session-id", state["session"]] if state["calls"] == 0 else ["--resume", state["session"]]
                proc = self._invoke(
                    role=role, tier=tier, model=model, tools=tools, schema=schema, prompt=prompt, packs=packs,
                    scratch=state["scratch"], patches=role in _PATCH_ROLES, session=session, budget_usd=budget_usd,
                    spent_usd=state.get("spent_usd", 0.0),
                )
            else:
                with self._scratch(role) as scratch:
                    proc = self._invoke(
                        role=role, tier=tier, model=model, tools=tools, schema=schema, prompt=prompt, packs=packs,
                        scratch=scratch, patches=True, session=(), budget_usd=budget_usd,
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
                    # A successful call clears any recorded stop, so a later
                    # unrelated call on this thread is not inflated by it.
                    state["spent_usd"] = 0.0
                break

            # Name everything the CLI said about it. A bare `None` result was
            # the whole diagnosis of a build failure once; never again.
            detail = {k: payload.get(k) for k in ("subtype", "result", "errors", "num_turns", "duration_ms") if payload.get(k) is not None}
            if attempt == 2 or not _is_transient(payload):
                message = f"node '{role}' failed in claude: {json.dumps(detail)[:800]}"
                if payload.get("subtype") == "error_max_budget_usd":
                    spent = float(payload.get("total_cost_usd") or 0.0)
                    partial_patch = ""
                    if thread:
                        # Leave `state` exactly as it was — same scratch, same
                        # `calls` — so a later `run(..., thread=same)` resumes
                        # this session instead of starting the node over.
                        state["spent_usd"] = spent
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

            # Keep the failed attempt's trace. The retry writes to the same
            # filename, and a transient error that leaves no record behind is
            # one nobody can measure the frequency of later.
            if payload.get("trace"):
                failed = Path(payload["trace"])
                failed.replace(failed.with_suffix(".error.jsonl"))

        data = payload.get("structured_output")
        if data is None:
            # An older build, or a session that answered in prose: the result
            # text is the last resort, and it has to parse or the node failed.
            try:
                data = json.loads(str(payload.get("result") or ""))
            except json.JSONDecodeError as exc:
                raise RunnerError(f"node '{role}' returned no structured output and its text is not JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise RunnerError(f"node '{role}' returned {type(data).__name__}, expected an object")

        usage = payload.get("usage") if isinstance(payload.get("usage"), Mapping) else {}
        self.calls.append(
            {
                "role": role,
                "tier": tier,
                "model": model,
                "tools": list(tools),
                "cost_usd": payload.get("total_cost_usd"),
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
            }
        )
        return NodeResult(data)
