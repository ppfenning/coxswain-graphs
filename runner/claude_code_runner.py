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
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from runner.protocol import NodeResult, RunnerError

__all__ = ["ClaudeCodeRunner", "TIER_EFFORT", "DEFAULT_TIER"]

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

    def _workspace(self) -> str:
        """Tell the node where the world is. It cannot find out on its own."""
        lines = [
            "<workspace>",
            f"Your working directory is {self.cwd}. It is the work store root: `work/` under it "
            "holds initiatives as work/<initiative>/<phase>/<task>.md.",
        ]
        if self.repo_dir:
            lines.append(
                f"The repository this run targets is checked out at {self.repo_dir}. Read it there. "
                "Any unified diff you return uses paths relative to that repository's root "
                "(with a/ and b/ prefixes) and is applied by the harness, never by you. "
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

    def _argv(self, *, model: str, tier: str, tools: Sequence[str], schema: Mapping[str, Any], system: str) -> list[str]:
        argv = [
            self.claude_bin,
            "-p",
            "--no-session-persistence",
            "--output-format",
            "json",
            "--model",
            model,
            "--effort",
            self.effort.get(tier, "high"),
            "--json-schema",
            json.dumps(dict(schema)),
            *_ISOLATION,
        ]
        if system:
            argv += ["--system-prompt", system]
        if self.repo_dir and self.repo_dir != self.cwd:
            argv += ["--add-dir", str(self.repo_dir)]
        if _WRITE_TOOLS & set(tools):
            argv += ["--permission-mode", "acceptEdits"]
        # Last on purpose: `--tools` is variadic, and nothing may follow it that
        # could be mistaken for a tool name. The prompt travels on stdin.
        argv += ["--tools", *(tools or [""])]
        return argv

    # ── execution ───────────────────────────────────────────────────────────

    def run(
        self,
        *,
        role: str,
        tier: str = DEFAULT_TIER,
        schema: Mapping[str, Any],
        prompt: str,
        context: Sequence[str] = (),
    ) -> NodeResult:
        model = self._model_for(tier)
        body = self.role_skills.get(role)
        packs = [body, *context] if body else list(context)
        system = "\n\n".join(
            part for part in (self._read_context(packs), self._workspace(), self.extra_system) if part
        )
        tools = self.tools.get(role, [])
        argv = self._argv(model=model, tier=tier, tools=tools, schema=schema, system=system)

        try:
            proc = subprocess.run(
                argv,
                input=prompt,
                capture_output=True,
                text=True,
                cwd=self.cwd,
                timeout=self.timeout,
            )
        except FileNotFoundError as exc:
            raise RunnerError(f"'{self.claude_bin}' not found; is Claude Code installed and on PATH?") from exc
        except subprocess.TimeoutExpired as exc:
            raise RunnerError(f"node '{role}' did not finish within {self.timeout}s") from exc

        stdout = (proc.stdout or "").strip()
        if not stdout:
            tail = (proc.stderr or "").strip()[-800:]
            raise RunnerError(f"node '{role}': claude exited {proc.returncode} with no output: {tail}")
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise RunnerError(f"node '{role}': claude output is not JSON: {stdout[:200]}") from exc
        if not isinstance(payload, dict):
            raise RunnerError(f"node '{role}': claude output is {type(payload).__name__}, expected an object")
        if payload.get("is_error"):
            # Name everything the CLI said about it. A bare `None` result was
            # the whole diagnosis of a build failure once; never again.
            detail = {k: payload.get(k) for k in ("subtype", "result", "errors", "num_turns", "duration_ms") if payload.get(k) is not None}
            raise RunnerError(f"node '{role}' failed in claude: {json.dumps(detail)[:800]}")

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
                "input_tokens": sum(
                    int(usage.get(k) or 0)
                    for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
                ),
                "output_tokens": int(usage.get("output_tokens") or 0),
            }
        )
        return NodeResult(data)
