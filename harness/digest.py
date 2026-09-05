"""A map of the repository, computed by tools, so a node does not pay to draw one.

The plan and build nodes spent most of their turns discovering the same thing:
what files exist, how big they are, which functions live where. Every turn
re-sends the whole conversation, so that discovery is the quadratic part of
the bill — 79 to 101 turns per build on the run that prompted this. `git
ls-files`, `wc` and `rg` answer the same questions for nothing, once, and the
answer goes into the node's system prompt before it spends a single turn.

Deterministic and dependency-light on purpose: git, wc and ripgrep, nothing
else. No model touches this. If ripgrep is missing the symbol index is
simply absent and the file map still stands — a digest that could not be
built is not an error, it is a node that has to look for itself.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections import defaultdict
from pathlib import Path

__all__ = ["MAX_DIGEST_CHARS", "build_digest"]

MAX_DIGEST_CHARS = 12_000

# Files whose contents are not worth indexing symbols from, and would swamp
# the digest if they were: markup, data, lockfiles, vendored bundles.
_SKIP_SYMBOLS = re.compile(r"\.(html?|css|svg|json|jsonl|lock|min\.js|csv|txt|pdf|png|jpe?g|gif|ico|woff2?|plist)$", re.I)

# One pattern per language family; `rg` runs them all at once.
_SYMBOL_PATTERNS = (
    r"^\s*(?:async\s+)?def\s+(\w+)",              # python
    r"^\s*class\s+(\w+)",                          # python / js / ts
    r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)",  # js / ts
    r"^(\w+)\s*\(\)\s*\{",                         # sh
    r"^\s*func\s+(?:\([^)]*\)\s*)?(\w+)",          # go
    r"^\s*(?:pub\s+)?fn\s+(\w+)",                  # rust
)


def _run(argv: list[str], cwd: Path) -> str:
    try:
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _line_counts(repo: Path, files: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for name in files:
        try:
            with open(repo / name, "rb") as fh:
                counts[name] = sum(1 for _ in fh)
        except OSError:
            counts[name] = 0
    return counts


def _symbols(repo: Path, files: list[str]) -> dict[str, list[str]]:
    """file -> ["name:line", ...] via one ripgrep pass. Empty when rg is absent."""
    if not shutil.which("rg"):
        return {}
    targets = [f for f in files if not _SKIP_SYMBOLS.search(f)]
    if not targets:
        return {}
    argv = ["rg", "--no-heading", "--line-number", "--only-matching", "--no-messages", "-o"]
    for pattern in _SYMBOL_PATTERNS:
        argv += ["-e", pattern]
    argv += ["--", *targets]
    out = _run(argv, repo)
    found: dict[str, list[str]] = defaultdict(list)
    for line in out.splitlines():
        # path:lineno:matched text
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        path, lineno, text = parts
        name = re.sub(r"^\W*(?:async\s+|export\s+|pub\s+)*(?:def|class|function|func|fn)\s+", "", text.strip())
        name = re.sub(r"[\s(].*$", "", name)
        if name:
            found[path].append(f"{name}:{lineno}")
    return dict(found)


def build_digest(repo: Path | str, *, max_chars: int = MAX_DIGEST_CHARS) -> str:
    """The digest text, or "" for anything that is not a git working tree."""
    repo = Path(repo)
    listing = _run(["git", "ls-files"], repo)
    files = [f for f in listing.splitlines() if f]
    if not files:
        return ""
    counts = _line_counts(repo, files)
    symbols = _symbols(repo, files)

    lines = [f"{len(files)} tracked files, {sum(counts.values())} lines. path (lines): symbols with line numbers"]
    for name in files:
        syms = symbols.get(name) or []
        head = f"{name} ({counts[name]})"
        lines.append(f"{head}: {' '.join(syms)}" if syms else head)
    text = "\n".join(lines)
    if len(text) > max_chars:
        # Drop symbols from the largest files first; the file map always survives.
        for name in sorted(files, key=lambda n: -len(symbols.get(n) or [])):
            if name in symbols:
                del symbols[name]
                text = "\n".join(
                    [lines[0]]
                    + [f"{n} ({counts[n]})" + (f": {' '.join(symbols[n])}" if symbols.get(n) else "") for n in files]
                )
                if len(text) <= max_chars:
                    break
        if len(text) > max_chars:
            text = text[: max_chars - 20] + "\n... (truncated)"
    return text
