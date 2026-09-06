"""Cartridge and skill resolution: everything the harness knows before a run."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from core.cartridge import load
from core.skills import index_from_roots

__all__ = ["overlay_path", "resolve_cartridge", "role_skill_bodies"]


def overlay_path(repo: str) -> str:
    return os.path.join(repo, ".agent", "cartridge.yaml")


class _Unverified(dict):
    """Every binding "resolves" — to itself. Only behind an explicit flag."""

    def get(self, key, default=None):
        return [key]


def resolve_cartridge(
    team: str,
    *,
    cartridges_dir: Path | str,
    skills_root: Sequence[str | Path],
    unverified_skills: bool = False,
    overlay: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], Mapping[str, Sequence[Any]]]:
    """Resolve the team's cartridge, and return the skill index used to do it.

    The index comes back alongside the cartridge because the harness needs it
    twice: once inside `load` to refuse bad bindings, and again afterwards to
    hand the live runner the skill BODIES those bindings resolved to. Building
    it twice would invite the two uses to drift.
    """
    index: Mapping[str, Sequence[Any]] = index_from_roots(skills_root)
    if unverified_skills:
        print("warning: skill bindings NOT verified (--unverified-skills)", file=sys.stderr)
        index = _Unverified()
    return load(team, cartridges_dir, skill_index=index, overlay=overlay), index


def role_skill_bodies(
    cartridge: Mapping[str, Any],
    index: Mapping[str, Sequence[Any]],
) -> dict[str, str]:
    """role -> absolute path of the skill body the cartridge bound to it.

    This is what makes a binding load-bearing rather than decorative: the live
    runner prepends the bound body to the node's system prompt. Resolution
    already guaranteed exactly one body per bound name; an unverified index
    yields no paths, so `--unverified-skills` runs carry no bodies — which is
    what unverified means.
    """
    bodies: dict[str, str] = {}
    for role, name in (cartridge.get("skills") or {}).items():
        found = list(index.get(name, ()))
        if len(found) == 1 and Path(str(found[0])).is_file():
            bodies[role] = str(found[0])
    return bodies
