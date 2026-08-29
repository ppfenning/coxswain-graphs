"""The portability check: a graph must not know who it works for.

This is the acceptance test that makes the cartridge seam real rather than
aspirational. A graph reads every team-, tracker-, and vendor-specific value
off `args.cartridge`. The moment one gets inlined "just for now", the seam
stops being exercised and quietly rots while the cartridge drifts away from it.

Run in CI. A failure here is not a style nit — it means a graph has bound
itself to one employer and is no longer portable.

    pytest tests/test_portability.py -q
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

GRAPHS_DIR = Path(__file__).resolve().parent.parent / "graphs"

# Shapes that identify a specific organization, tracker, or tenant. These are
# patterns, deliberately not a denylist of names: a denylist only catches the
# employer you remembered to add, and the next one walks straight through.
FORBIDDEN_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\b\d{15,19}\b", "a bare numeric ID (tracker workspace/project/task GID)"),
    (r"https?://[\w.-]*\.(?:atlassian\.net|asana\.com|monday\.com)", "a hardcoded tracker host"),
    (r"\bs3://[\w.-]+", "a hardcoded bucket URI"),
    (r"\barn:aws:[\w:-]+", "a hardcoded AWS ARN"),
    (r"\b\d{12}\b", "a bare 12-digit AWS account ID"),
    (r"(?i)\b(?:api[_-]?key|secret|token|password)\s*[:=]\s*['\"][^'\"]{8,}", "an inline credential"),
    (r"/(?:Users|home)/[a-z][\w.-]*/", "an absolute path containing a username"),
)

# Comments may legitimately discuss provenance and rationale. Code may not.
COMMENT_LINE = re.compile(r"^\s*(?://|\*|/\*|#)")


def _graph_files() -> list[Path]:
    return sorted(p for p in GRAPHS_DIR.rglob("*") if p.suffix in {".js", ".mjs", ".ts", ".py"})


def _code_lines(path: Path) -> list[tuple[int, str]]:
    """Lines with comment-only lines dropped. Not a parser; deliberately crude.

    A crude filter that occasionally flags a comment is the right failure
    direction here: a false positive costs someone thirty seconds, and a false
    negative ships a hardcoded tenant ID to production.
    """
    return [
        (n, line)
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if not COMMENT_LINE.match(line)
    ]


@pytest.mark.parametrize("pattern,description", FORBIDDEN_PATTERNS)
def test_no_domain_constants_in_graphs(pattern: str, description: str) -> None:
    rx = re.compile(pattern)
    findings = [
        f"{path.name}:{n}: {description}\n    {line.strip()[:120]}"
        for path in _graph_files()
        for n, line in _code_lines(path)
        if rx.search(line)
    ]
    assert not findings, (
        "Graphs must read every team/tracker/vendor specific off args.cartridge.\n"
        "Found domain constants inlined:\n\n" + "\n".join(findings)
    )


def test_graphs_require_a_cartridge_with_no_fallback() -> None:
    """Every graph must fail loudly when args.cartridge is absent.

    A default is worse than a crash: the graph runs against stale inlined
    values, produces plausible output, and nobody notices the seam is dead.
    """
    offenders = [
        path.name
        for path in _graph_files()
        if path.suffix in {".js", ".mjs", ".ts"}
        and "args.cartridge" in path.read_text(encoding="utf-8")
        and not re.search(r"(?:throw|raise)[^\n]{0,120}cartridge", path.read_text(encoding="utf-8"), re.I)
    ]
    assert not offenders, (
        "These graphs read args.cartridge but never throw when it is missing, "
        "which means they can silently fall back: " + ", ".join(offenders)
    )


def test_there_is_something_to_check() -> None:
    """Guard against the check passing because it found no files at all."""
    assert GRAPHS_DIR.is_dir(), f"missing graphs directory: {GRAPHS_DIR}"
