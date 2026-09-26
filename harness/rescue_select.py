"""Which quarantined tasks a rescue may take, and where its patch comes from. Pure: no I/O, no clock."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.workstore import attempts_on_current_body

RESCUE_KIND = "rescue_failed"


def patch_of(result: Mapping[str, Any] | None) -> str | None:
    """The non-blank build.patch. A budget stop keeps its last reviewed build there too, with fix_loop.stopped set."""
    build = result.get("build") if isinstance(result, Mapping) else None
    patch = build.get("patch") if isinstance(build, Mapping) else None
    return patch if isinstance(patch, str) and patch.strip() else None


def eligible(item: Mapping[str, Any], patch: str | None) -> tuple[bool, str]:
    """Whether a rescue may run: a kept patch, a harness cause last, and no rescue yet on this ticket text."""
    if patch is None or not patch.strip():
        return False, "no patch kept"
    attempts = list(attempts_on_current_body(item))
    if not attempts:
        return False, "no attempts on the current body"
    if attempts[-1].get("cause") != "harness":
        return False, f"last cause is {attempts[-1].get('cause')}, not harness"
    if any(a.get("kind") == RESCUE_KIND for a in attempts):
        return False, "a rescue already failed on this ticket version"
    return True, "harness cause, patch kept"


def rescue_cause(failed_checks: bool) -> tuple[str, str]:
    """The rule-made (cause, cause_why) for a rescue_failed attempt."""
    if failed_checks:
        return "code", "rule: rescue checks failed"
    return "review", "rule: rescue review revised"
