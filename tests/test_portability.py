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
        if "args.cartridge" in path.read_text(encoding="utf-8")
        and not re.search(r"(?:throw|raise)[^\n]{0,120}cartridge", path.read_text(encoding="utf-8"), re.I)
    ]
    assert not offenders, (
        "These graphs read args.cartridge but never throw when it is missing, "
        "which means they can silently fall back: " + ", ".join(offenders)
    )


# The Python spelling of a fallback. `args.get("cartridge")` with a second
# argument is exactly the silent default the contract forbids, and it does not
# look like one at a glance — which is why it gets its own pattern.
CARTRIDGE_FALLBACK = re.compile(r"""\.get\(\s*['"]cartridge['"]\s*,""")


def test_no_python_graph_defaults_the_cartridge() -> None:
    findings = [
        f"{path.name}:{n}: {line.strip()[:120]}"
        for path in _graph_files()
        if path.suffix == ".py"
        for n, line in _code_lines(path)
        if CARTRIDGE_FALLBACK.search(line)
    ]
    assert not findings, (
        "A cartridge default means the seam is never exercised and rots silently:\n" + "\n".join(findings)
    )


def _graph_modules():
    """Import every module in graphs/ that exposes a graph entrypoint."""
    import importlib
    import sys

    sys.path.insert(0, str(GRAPHS_DIR.parent))
    modules = []
    for path in _graph_files():
        if path.suffix != ".py" or path.stem.startswith("__"):
            continue
        dotted = ".".join(("graphs", *path.relative_to(GRAPHS_DIR).with_suffix("").parts))
        module = importlib.import_module(dotted)
        if hasattr(module, "run") and hasattr(module, "GRAPH_NAME"):
            modules.append(module)
    return modules


def test_every_graph_actually_refuses_to_run_without_a_cartridge() -> None:
    """The behavioural half of the rule above.

    Scanning text catches an inlined fallback; only running the thing catches a
    graph that forgot to ask for a cartridge at all. A grep-only check would
    pass happily on a graph that never mentions the word.
    """
    from graphs._contract import ContractViolation

    modules = _graph_modules()
    assert modules, "no graph modules found — the check would pass by finding nothing"
    for module in modules:
        # ContractViolation specifically, not any exception: a graph that dies of
        # an AttributeError on `args.cartridge` also "fails loudly", but it did so
        # by accident. Refusing on purpose is the behaviour under test.
        with pytest.raises(ContractViolation) as exc:
            module.run({"run_id": "r", "date": "2026-01-01"}, runner=None)
        assert "cartridge" in str(exc.value).lower(), (
            f"{module.GRAPH_NAME} refused, but not because a cartridge was missing: {exc.value}"
        )


def test_there_is_something_to_check() -> None:
    """Guard against the check passing because it found no files at all."""
    assert GRAPHS_DIR.is_dir(), f"missing graphs directory: {GRAPHS_DIR}"
    assert _graph_files(), f"no graph source files under {GRAPHS_DIR}"
