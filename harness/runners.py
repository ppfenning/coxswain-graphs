"""Runner construction: which execution backend this run gets."""

from __future__ import annotations

import importlib.metadata
import json
import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from runner import ScriptedRunner
from runner.system_one import ConfigError, DecisionRunner, FastPathRunner, SystemOneConfig, parse_system_one_block
from runner.system_one_specs import role_specs

__all__ = ["build_runner"]


class _Unavailable(Exception):
    """The block is valid but its backend cannot be served now. The message is the one-line reason."""


ENTRY_POINT_GROUP = "coxswain.system_one"


def _backend(name: str) -> Callable[[str, str, Mapping[str, Any]], DecisionRunner] | None:
    """The decider constructor a plugin registers under `name` in the entry-point group, or None.

    A constructor takes the model, the api key and the whole `system_one` block. One that calls a
    hosted API sets `needs_key = True` and is given the key from `system_one.key_env`.
    """
    for entry in importlib.metadata.entry_points(group=ENTRY_POINT_GROUP):
        if entry.name == name:
            try:
                return entry.load()
            except ImportError as error:
                raise _Unavailable(str(error)) from error
    return None


def _active_config(profile: Mapping[str, Any]) -> SystemOneConfig | None:
    """The parsed `system_one` block, or None when it is absent or every role is off. A bad block raises."""
    block = profile.get("system_one")
    if block is None:
        return None
    config = parse_system_one_block(block)
    if isinstance(config, ConfigError):
        raise ValueError(f"system_one: {config.error}")
    return None if all(s.mode == "off" for s in config.roles.values()) else config


def _decider(config: SystemOneConfig, profile: Mapping[str, Any]) -> DecisionRunner:
    constructor = _backend(config.backend)
    if constructor is None:
        raise _Unavailable(
            f"system_one.backend {config.backend!r} is not registered; "
            f"install the plugin that provides it (entry-point group {ENTRY_POINT_GROUP})"
        )
    # A hosted backend's key comes from its own `system_one.key_env`, never the provider's
    # `auth_env`: the provider's key must not be sent to a third party's API.
    api_key = ""
    if getattr(constructor, "needs_key", False):
        env_var = profile["system_one"].get("key_env")
        if not isinstance(env_var, str) or not env_var:
            raise _Unavailable(f"system_one.key_env names no environment variable for backend {config.backend}")
        api_key = os.environ.get(env_var, "")
        if not api_key:
            raise _Unavailable(f"${env_var} (system_one.key_env) is not set")
    try:
        return constructor(config.model, api_key, profile["system_one"])
    except ImportError as error:
        raise _Unavailable(str(error)) from error


class _DelegatingFastPath(FastPathRunner):
    """A FastPathRunner that forwards every public attribute read and write to the inner runner.

    The wrapper is not a drop-in runner on its own. The harness configures its runner after
    build_runner returns, by `hasattr` then assignment: run_id, node_cap_usd, repo_dir,
    check_commands, verify_by_task, and it reads `calls` and `close`. Without this forwarding
    each `hasattr` is False on the wrapper, and the per-node USD cap is dropped with no error.
    Underscored names stay on the wrapper, which owns `_inner`, `_decider` and the rest.
    """

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._inner, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
        else:
            setattr(self._inner, name, value)


def _to_stderr(line: str) -> None:
    print(line, file=sys.stderr)


def _with_fast_path(real: Any, profile: Mapping[str, Any], notes: Callable[[str], None] = _to_stderr) -> Any:
    """`real` itself unless the profile turns a role on, then `real` wrapped in the fast path.

    A malformed block raises. A valid block whose backend is unavailable leaves `real` unwrapped
    and reports one `system-one: off (<reason>)` line to `notes`.
    """
    config = _active_config(profile)
    if config is None:
        return real
    try:
        decider = _decider(config, profile)
    except _Unavailable as unavailable:
        notes(f"system-one: off ({unavailable})")
        return real
    return _DelegatingFastPath(real, decider, config.roles, role_specs(), backend=config.backend)


def build_runner(
    *,
    scripted: str | Path | None,
    provider_profile: str | Path,
    role_skills: Mapping[str, str] | None = None,
    workdir: str | Path | None = None,
    repo: str | Path | None = None,
    notes: Callable[[str], None] = _to_stderr,
) -> Any:
    """A ScriptedRunner from canned responses, or one of the live runners.

    The live imports stay inside their branches: the scripted path must work on
    a machine with no SDK installed, because that is the whole point of it.

    `role_skills` maps role -> bound skill body path, resolved by the harness.
    The scripted runner ignores it — canned responses already ARE the node's
    output — but the live runners prepend the body to the node's system, which
    is the moment a cartridge binding stops being a validated name and starts
    being what the node actually knows.

    Which live runner is the PROFILE's call (`runner: claude-code` selects the
    headless Claude Code runner; anything else is the Messages API), because the
    vendor axis is the profile's whole job and a CLI flag would be a second copy
    of it. `workdir` and `repo` matter only to a runner whose nodes can read
    the world: the work store root the arms write under, and the repository the
    build and review roles read.
    """
    if scripted:
        responses = json.loads(Path(scripted).read_text(encoding="utf-8"))
        return ScriptedRunner(responses)

    from runner.anthropic_runner import load_provider_profile

    profile = load_provider_profile(provider_profile)
    if profile.get("runner") == "claude-code":
        from runner.claude_code_runner import ClaudeCodeRunner

        real: Any = ClaudeCodeRunner(
            profile,
            role_skills=role_skills or {},
            cwd=workdir,
            repo_dir=repo,
            trace_dir=os.environ.get("AGENT_GRAPHS_TRACE_DIR") or None,
        )
    else:
        from runner.anthropic_runner import AnthropicRunner

        real = AnthropicRunner(profile, role_skills=role_skills or {})
    return _with_fast_path(real, profile, notes)
