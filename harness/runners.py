"""Runner construction: which execution backend this run gets."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from runner import ScriptedRunner
from runner.system_one import ConfigError, DecisionRunner, FastPathRunner, SystemOneConfig, parse_system_one_block
from runner.system_one_specs import role_specs

__all__ = ["build_runner"]


def _jev(model: str, api_key: str, block: Mapping[str, Any]) -> DecisionRunner:
    from runner.system_one_jev import JevDecider

    return JevDecider(model, api_key)


def _knn_local(model: str, api_key: str, block: Mapping[str, Any]) -> DecisionRunner:
    """Reads `examples` (a path), `k` (default 5), `device` (cpu or cuda) and `embedding_model` from the block."""
    from runner.system_one_knn import DEFAULT_EMBEDDING_MODEL, load_knn_decider

    if not block.get("examples"):
        raise ValueError("system_one.examples must name the examples file for backend knn-local")
    return load_knn_decider(
        block["examples"],
        block.get("k", 5),
        device=block.get("device", "cpu"),
        model_name=block.get("embedding_model", DEFAULT_EMBEDDING_MODEL),
    )


# Backend name to decider constructor, given the model, the api key and the whole `system_one` block.
# A new backend is one entry here.
_BACKENDS: dict[str, Callable[[str, str, Mapping[str, Any]], DecisionRunner]] = {"jev": _jev, "knn-local": _knn_local}
# Backends that call a hosted API. The others run locally and need no key.
_NEEDS_KEY = frozenset({"jev"})


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
    constructor = _BACKENDS.get(config.backend)
    if constructor is None:
        raise ValueError(f"system_one.backend {config.backend!r} is unknown; known: {', '.join(sorted(_BACKENDS))}")
    env_var = profile.get("auth_env", "ANTHROPIC_API_KEY")
    api_key = os.environ.get(env_var, "")
    if config.backend in _NEEDS_KEY and not api_key:
        raise ValueError(f"system_one needs ${env_var} (the profile's auth_env), and it is not set")
    return constructor(config.model, api_key, profile["system_one"])


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


def _with_fast_path(real: Any, profile: Mapping[str, Any]) -> Any:
    """`real` itself unless the profile turns a role on, then `real` wrapped in the fast path."""
    config = _active_config(profile)
    if config is None:
        return real
    return _DelegatingFastPath(real, _decider(config, profile), config.roles, role_specs(), backend=config.backend)


def build_runner(
    *,
    scripted: str | Path | None,
    provider_profile: str | Path,
    role_skills: Mapping[str, str] | None = None,
    workdir: str | Path | None = None,
    repo: str | Path | None = None,
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
    return _with_fast_path(real, profile)
