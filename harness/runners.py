"""Runner construction: which execution backend this run gets."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from runner import ScriptedRunner

__all__ = ["build_runner"]


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
        import os

        from runner.claude_code_runner import ClaudeCodeRunner

        return ClaudeCodeRunner(
            profile,
            role_skills=role_skills or {},
            cwd=workdir,
            repo_dir=repo,
            trace_dir=os.environ.get("AGENT_GRAPHS_TRACE_DIR") or None,
        )

    from runner.anthropic_runner import AnthropicRunner

    return AnthropicRunner(profile, role_skills=role_skills or {})
