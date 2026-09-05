"""What a graph declares so a harness can offer it: the spec types.

These live beside `_contract.py` rather than in the harness because they are
part of the GRAPH's contract — a graph declares a `SPEC`, and stays importable
with no harness and no substrate installed. That property is load-bearing: the
graph test suite (and CI without the private substrate checkout) imports graph
modules directly, and a spec type that pulled in the harness would pull in
`core` behind it.

A spec is declarative on purpose. It names the graph's inputs and how a CLI
should obtain them (`Need`); it never reads a file or parses anything itself.
The contract gives graphs no filesystem access, so the harness performs the
I/O the needs describe, and the graph side stays pure enough for the
portability suite to hold it to that.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = ["GraphSpec", "Need"]


@dataclass(frozen=True)
class Need:
    """One input a graph requires the harness to supply.

    kind:
        str           pass the flag's value through
        int           integer
        json_file     the flag names a JSON file; the graph gets the parsed data
        jsonl_file    the flag names a JSON-Lines file; the graph gets the parsed rows
        text_or_path  prose, or a path to a file holding it (read if it exists)
    """

    name: str
    flag: str
    kind: str = "str"
    required: bool = True
    help: str = ""


@dataclass(frozen=True)
class GraphSpec:
    """What the harness needs to offer a graph as a subcommand."""

    name: str                      # the subcommand
    graph_name: str                # the graph's own GRAPH_NAME, for reporting
    run: Callable[[Mapping[str, Any], Any], dict[str, Any]]
    summary: str = ""
    needs: tuple[Need, ...] = field(default=())
