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
) -> Any:
    """A ScriptedRunner from canned responses, or the live runner.

    The live import stays inside the branch: the scripted path must work on a
    machine with no SDK installed, because that is the whole point of it.

    `role_skills` maps role -> bound skill body path, resolved by the harness.
    The scripted runner ignores it — canned responses already ARE the node's
    output — but the live runner prepends the body to the node's system, which
    is the moment a cartridge binding stops being a validated name and starts
    being what the node actually knows.
    """
    if scripted:
        responses = json.loads(Path(scripted).read_text(encoding="utf-8"))
        return ScriptedRunner(responses)

    from runner.anthropic_runner import AnthropicRunner, load_provider_profile

    return AnthropicRunner(load_provider_profile(provider_profile), role_skills=role_skills or {})
