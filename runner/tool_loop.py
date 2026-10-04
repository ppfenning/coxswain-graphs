"""A vendor-neutral tool loop: drive an adapter through a worktree, return its diff.

The loop knows no SDK and no wire format. An adapter turns the neutral
`messages` and `tools` into its own request and the reply into an `AdapterStep`.

Contract for adapters, which the loop relies on:
- `messages[0]` is the system message, `{"role": "system", "content", "schema"}`;
  the final answer's JSON schema rides there because `send` has no schema argument.
- Later messages are `{"role": "user", "content"}`, `{"role": "assistant",
  "tool_calls": [{"id", "name", "arguments"}]}` and one `{"role": "tool",
  "tool_call_id", "content", "is_error"}` per executed call.
- `AdapterStep.usage` is a flat dict of numbers. The loop sums every key across
  steps and compares the `cost_usd` total against `budget_usd`.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from runner.claude_code_runner import (
    _VERIFY_TAIL_LINES,
    _VERIFY_TIMEOUT_S,
    _VERIFY_TOTAL_S,
    _capture_diff,
    reconcile_patch,
)
from runner.protocol import BudgetStop

COST_KEY = "cost_usd"


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass(frozen=True)
class ToolResult:
    id: str
    content: str
    is_error: bool = False


@dataclass(frozen=True)
class FinalAnswer:
    data: dict


@dataclass(frozen=True)
class AdapterStep:
    tool_calls: list[ToolCall]
    final: FinalAnswer | None
    usage: dict


class ToolLoopAdapter(Protocol):
    def send(self, messages: list[dict], tools: list[dict]) -> AdapterStep: ...


@dataclass(frozen=True)
class LoopResult:
    """`stop_reason` is "final" or "turn_cap"; a budget stop raises `BudgetStop` instead."""

    patch: str
    stop_reason: str
    usage: dict
    patch_error: str | None = None


class ToolError(Exception):
    """A tool call the model got wrong; the dispatcher shows it back as an error result."""


def _tool(name: str, description: str, properties: dict[str, dict], required: list[str]) -> dict:
    return {
        "name": name,
        "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required},
    }


_PATH = {"type": "string", "description": "Path relative to the worktree root."}

TOOLS: list[dict] = [
    _tool(
        "read_file",
        "Read a text file. `offset` is the number of lines to skip; `limit` is the most lines to return.",
        {"path": _PATH, "offset": {"type": "integer"}, "limit": {"type": "integer"}},
        ["path"],
    ),
    _tool("list_dir", "List the immediate entries of a directory.", {"path": _PATH}, ["path"]),
    _tool(
        "write_file",
        "Write a file, creating parent directories. Replaces any existing content.",
        {"path": _PATH, "content": {"type": "string"}},
        ["path", "content"],
    ),
    _tool(
        "edit_file",
        "Replace `old` with `new` in a file. `old` must occur exactly once.",
        {"path": _PATH, "old": {"type": "string"}, "new": {"type": "string"}},
        ["path", "old", "new"],
    ),
    _tool(
        "run_check",
        "Run one of the pre-approved check commands in the worktree. Any other command is refused.",
        {"command": {"type": "string"}},
        ["command"],
    ),
]


def _resolve_in_worktree(worktree: Path, path: str) -> Path:
    if Path(path).is_absolute():
        raise ToolError(f"absolute path refused: {path}")
    target = (worktree / path).resolve()
    if not target.is_relative_to(worktree.resolve()):
        raise ToolError(f"path is outside the worktree: {path}")
    return target


def _read_file(worktree: Path, path: str, offset: int = 0, limit: int | None = None) -> str:
    lines = _resolve_in_worktree(worktree, path).read_text().splitlines(keepends=True)
    start = max(offset, 0)
    return "".join(lines[start:] if limit is None else lines[start : start + max(limit, 0)])


def _list_dir(worktree: Path, path: str) -> str:
    entries = sorted(_resolve_in_worktree(worktree, path).iterdir(), key=lambda p: p.name)
    return "\n".join(p.name + ("/" if p.is_dir() else "") for p in entries)


def _write_file(worktree: Path, path: str, content: str) -> str:
    target = _resolve_in_worktree(worktree, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    return f"wrote {len(content)} characters to {path}"


def _edit_file(worktree: Path, path: str, old: str, new: str) -> str:
    target = _resolve_in_worktree(worktree, path)
    text = target.read_text()
    count = text.count(old) if old else 0
    if count == 0:
        raise ToolError(f"old text not found in {path}")
    if count > 1:
        raise ToolError(f"old text is not unique in {path}: {count} occurrences")
    target.write_text(text.replace(old, new, 1))
    return f"edited {path}"


class _CheckClock:
    """Wall time `run_check` has spent so far in one loop call. Mutated at the edge, once per check."""

    def __init__(self) -> None:
        self.spent_s = 0.0


def _run_check(call_id: str, worktree: Path, command: str, checks: Sequence[str], clock: _CheckClock) -> ToolResult:
    if command not in checks:
        return ToolResult(call_id, f"refused: {command!r} is not one of the allowed checks", True)
    remaining = _VERIFY_TOTAL_S - clock.spent_s
    if remaining <= 0:
        return ToolResult(call_id, f"refused: the {_VERIFY_TOTAL_S}s check budget is spent, {command!r} not run", True)
    timeout = min(_VERIFY_TIMEOUT_S, remaining)
    started = time.monotonic()
    try:
        proc = subprocess.run(command, shell=True, cwd=worktree, capture_output=True, text=True, timeout=timeout)
        tail = "\n".join(((proc.stdout or "") + (proc.stderr or "")).splitlines()[-_VERIFY_TAIL_LINES:])
        result = ToolResult(call_id, f"{tail}\n(exit {proc.returncode})")
    except subprocess.TimeoutExpired:
        result = ToolResult(call_id, f"timed out after {timeout:g}s: {command!r}", True)
    except OSError as exc:
        result = ToolResult(call_id, f"could not start {command!r}: {exc}", True)
    clock.spent_s += time.monotonic() - started
    return result


def _execute(call: ToolCall, worktree: Path, checks: Sequence[str], clock: _CheckClock) -> ToolResult:
    """One tool call to one result, never a raise: a bad call is an error the model reads."""
    args: dict[str, Any] = call.arguments
    try:
        if call.name == "run_check":
            return _run_check(call.id, worktree, args["command"], checks, clock)
        if call.name == "read_file":
            content = _read_file(worktree, args["path"], args.get("offset", 0), args.get("limit"))
        elif call.name == "list_dir":
            content = _list_dir(worktree, args["path"])
        elif call.name == "write_file":
            content = _write_file(worktree, args["path"], args["content"])
        elif call.name == "edit_file":
            content = _edit_file(worktree, args["path"], args["old"], args["new"])
        else:
            return ToolResult(call.id, f"unknown tool: {call.name}", True)
    except (ToolError, OSError, KeyError, TypeError, ValueError) as exc:
        return ToolResult(call.id, f"{type(exc).__name__}: {exc}", True)
    return ToolResult(call.id, content)


def _add_usage(total: dict, step: dict) -> dict:
    return {k: total.get(k, 0) + step.get(k, 0) for k in {*total, *step}}


def _reconciled(worktree: Path, reported: str) -> tuple[str, str | None]:
    return reconcile_patch(reported, _capture_diff(worktree))


def run_tool_loop(
    adapter: ToolLoopAdapter,
    *,
    worktree: Path,
    system_prompt: str,
    user_prompt: str,
    schema: dict,
    checks: Sequence[str],
    turn_cap: int,
    budget_usd: float | None,
    wait_if_paused: Callable[[], None] | None = None,
) -> LoopResult:
    """Drive `adapter` until it answers, `turn_cap` turns pass, or `budget_usd` is exceeded.

    A budget stop raises `BudgetStop` carrying the worktree's diff as `partial_patch`.
    """
    # The edge: the message list and the clock are local and mutated per turn.
    messages: list[dict] = [
        {"role": "system", "content": system_prompt, "schema": schema},
        {"role": "user", "content": user_prompt},
    ]
    clock = _CheckClock()
    usage: dict = {}
    final: FinalAnswer | None = None
    turns = 0
    while turns < turn_cap and final is None:
        if wait_if_paused is not None:
            wait_if_paused()
        step = adapter.send(messages, TOOLS)
        turns += 1
        usage = _add_usage(usage, step.usage)
        spent = float(usage.get(COST_KEY, 0.0))
        if budget_usd is not None and spent > budget_usd:
            partial, _ = _reconciled(worktree, "")
            raise BudgetStop(
                role="tool_loop",
                thread=None,
                session=None,
                spent_usd=spent,
                detail=f"tool loop spent ${spent:.4f} over the ${budget_usd:.4f} budget after {turns} turns",
                partial_patch=partial,
                num_turns=turns,
            )
        if step.final is not None:
            final = step.final
            continue
        calls = [{"id": c.id, "name": c.name, "arguments": c.arguments} for c in step.tool_calls]
        messages.append({"role": "assistant", "tool_calls": calls})
        for call in step.tool_calls:
            result = _execute(call, worktree, checks, clock)
            messages.append(
                {"role": "tool", "tool_call_id": result.id, "content": result.content, "is_error": result.is_error}
            )
    reported = str(final.data.get("patch") or "") if final is not None else ""
    patch, patch_error = _reconciled(worktree, reported)
    return LoopResult(patch, "final" if final is not None else "turn_cap", usage, patch_error)
