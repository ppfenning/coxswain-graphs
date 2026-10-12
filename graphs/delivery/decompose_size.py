"""Pure helpers for the decompose size step: verdict parsing, override, one-ticket initiative."""

from __future__ import annotations

import re
from dataclasses import dataclass

VERDICTS = ("task", "epic")
NEW_MARKER = " (new)"
# A terminator then more text ends a sentence, unless the token before it is an abbreviation.
_TERMINATOR = re.compile(r"(?<!\S)(\S+)[.!?]\s+(?=\S)")
_ABBREVIATIONS = frozenset({"e.g", "i.e", "etc", "vs", "cf", "approx", "mr", "mrs", "ms", "dr", "st", "no"})


@dataclass(frozen=True)
class SizeVerdict:
    verdict: str
    reason: str
    surfaces: tuple[str, ...]
    source: str  # "model" or "override"


@dataclass(frozen=True)
class SizeError:
    message: str


def _is_abbreviation(token: str) -> bool:
    word = token.lower().lstrip("(\"'")
    return word in _ABBREVIATIONS or len(word) == 1


def is_one_sentence(text: str) -> bool:
    return all(_is_abbreviation(m.group(1)) for m in _TERMINATOR.finditer(text))


def parse_size_verdict(raw: dict) -> SizeVerdict | SizeError:
    if not isinstance(raw, dict):
        return SizeError("size output is not an object")
    verdict = raw.get("verdict")
    if verdict not in VERDICTS:
        return SizeError(f"verdict must be task or epic, got {verdict!r}")
    reason = raw.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return SizeError("reason must be a non-empty string")
    if not is_one_sentence(reason.strip()):
        return SizeError("reason must be one sentence")
    if verdict == "epic":
        return SizeVerdict("epic", reason.strip(), (), "model")
    surfaces = raw.get("surfaces")
    if not isinstance(surfaces, list) or not all(isinstance(s, str) and s for s in surfaces):
        return SizeError("a task verdict needs surfaces as a list of paths")
    if not surfaces:
        return SizeError("a task verdict needs at least one surface")
    return SizeVerdict("task", reason.strip(), tuple(surfaces), "model")


def apply_override(verdict: SizeVerdict | SizeError, intake_frontmatter: dict) -> SizeVerdict | SizeError:
    """Widened to SizeError for a bad `size` value; an epic carries no surfaces, a task keeps the model's."""
    if "size" not in intake_frontmatter:
        return verdict
    size = intake_frontmatter["size"]
    if size not in VERDICTS:
        return SizeError(f"size override must be task or epic, got {size!r}")
    surfaces = verdict.surfaces if size == "task" and isinstance(verdict, SizeVerdict) else ()
    return SizeVerdict(size, f"intake frontmatter sets size: {size}", surfaces, "override")


def _exists(surface: str, repo_files: set[str]) -> bool:
    path = surface.removeprefix("./")
    if path.endswith("/"):
        return any(f.startswith(path) for f in repo_files)
    return path in repo_files


def mark_new_surfaces(surfaces: list[str], repo_files: set[str]) -> list[str]:
    """Idempotent: an already-marked surface is kept; a trailing `/` is a directory, existing if any file is under it."""
    return [s if s.endswith(NEW_MARKER) or _exists(s, repo_files) else f"{s}{NEW_MARKER}" for s in surfaces]


def ticket_slug(intake_id: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", intake_id.lower()).strip("-")


def single_ticket_initiative(intake: dict, verdict: SizeVerdict, repo_files: set[str]) -> dict | SizeError:
    """The task carries `checks`, which DECOMPOSE_SCHEMA does not list; the wiring item must accept it."""
    missing = [k for k in ("id", "title", "body") if not isinstance(intake.get(k), str) or not intake[k].strip()]
    slug = ticket_slug(intake["id"]) if "id" not in missing else ""
    if missing:
        return SizeError(f"intake is missing {', '.join(missing)}")
    if not slug:
        return SizeError(f"intake id {intake['id']!r} has no letters or digits to slug")
    if verdict.verdict != "task":
        return SizeError("a single-ticket initiative needs a task verdict")
    if not verdict.surfaces:
        return SizeError("a task verdict with no surfaces cannot become a ticket")
    return {
        "phases": [{"id": "p1", "goal": intake["title"]}],
        "tasks": [
            {
                "id": slug,
                "phase": "p1",
                "title": intake["title"],
                "body": intake["body"],
                "needs": [],
                "surfaces": mark_new_surfaces(list(verdict.surfaces), repo_files),
                "checks": list(intake.get("checks") or []),
            }
        ],
        "rationale": verdict.reason,
    }
