"""Mermaid diagrams and pages, generated from each graph's own docstring.

`graphs._spec.GraphSpec` (see `harness/registry.py`) carries what a CLI needs
to run a graph — its name, its `Need`s, a one-line summary — not its node
shape. The node chain already lives somewhere else in the tree: every graph
module opens with a docstring whose second line is exactly that chain
(`decompose -> adversary -> emit`), because a human reading the module wants
to see the shape before the code. Reading it back is the only way a diagram
can be generated FROM the graph rather than authored beside it, which is the
property that keeps it from drifting.

`Spec` is the small, literal value the pure functions here take: a name and
that docstring text, nothing else. `for_graph` is the one edge function, and
it is the only thing in this module that imports anything — it turns a
discovered `GraphSpec` into a `Spec` by reading the module it came from.
"""

from __future__ import annotations

import argparse
import importlib
from dataclasses import dataclass
from pathlib import Path

from harness.registry import GraphSpec, discover

__all__ = ["Spec", "for_graph", "mermaid", "page", "main"]


@dataclass(frozen=True)
class Spec:
    """What graphdoc needs to render one graph: its name and its docstring text."""

    name: str
    doc: str


def _paragraphs(doc: str) -> list[str]:
    """`doc` split on blank lines, each paragraph's own lines joined to one."""
    groups: list[list[str]] = [[]]
    for line in doc.strip("\n").splitlines():
        if line.strip():
            groups[-1].append(line.strip())
        elif groups[-1]:
            groups.append([])
    return [" ".join(group) for group in groups if group]


def _chain(doc: str) -> list[str]:
    """The node chain: the paragraph right after the title, `a -> b -> c`.

    An optional branch is written `[a -> b]`; the brackets are dropped and the
    branch folds into the main sequence, so the diagram shows one path through
    rather than a shape a reader could not act on anyway.
    """
    paragraphs = _paragraphs(doc)
    if len(paragraphs) < 2:
        return []
    chain = paragraphs[1].replace("[", "").replace("]", "")
    return [node.strip() for node in chain.split("->") if node.strip()]


def _description(doc: str) -> str:
    """The first prose paragraph: the one after the title and the chain."""
    paragraphs = _paragraphs(doc)
    return paragraphs[2] if len(paragraphs) > 2 else ""


def mermaid(spec: Spec) -> str:
    """A `flowchart LR`: one node per chain entry, one arrow per consecutive pair.

    Each node is labelled `<node><br/>step · <n>` — the chain carries no other
    per-node structure to label it with, so its position in the sequence is
    what the label reports. The entry (the first node) is styled distinctly.
    """
    nodes = _chain(spec.doc)
    ids = [f"n{i}" for i in range(len(nodes))]
    lines = ["flowchart LR"]
    lines.extend(
        f'    {node_id}["{name}<br/>step · {i + 1}"]' for i, (node_id, name) in enumerate(zip(ids, nodes))
    )
    lines.extend(f"    {a} --> {b}" for a, b in zip(ids, ids[1:]))
    if ids:
        lines.append(f"    style {ids[0]} fill:#f96,stroke:#333,stroke-width:2px")
    return "\n".join(lines)


def page(spec: Spec) -> str:
    """A markdown page: H1, description, the mermaid fence, then a node table."""
    nodes = _chain(spec.doc)
    rows = "\n".join(f"| `{name}` | {i + 1} |" for i, name in enumerate(nodes))
    return (
        f"# {spec.name}\n\n"
        f"{_description(spec.doc)}\n\n"
        f"```mermaid\n{mermaid(spec)}\n```\n\n"
        f"| Node | Step |\n|---|---|\n{rows}\n"
    )


def for_graph(graph_spec: GraphSpec) -> Spec:
    """The edge: read the docstring of the module a discovered GraphSpec came from."""
    module = importlib.import_module(graph_spec.run.__module__)
    return Spec(name=graph_spec.graph_name, doc=module.__doc__ or "")


def _write_all(out: Path, specs: dict[str, GraphSpec]) -> list[str]:
    graph_specs = sorted(specs.values(), key=lambda spec: spec.graph_name)
    doc_specs = [for_graph(graph_spec) for graph_spec in graph_specs]
    for doc_spec in doc_specs:
        (out / f"{doc_spec.name}.md").write_text(page(doc_spec), encoding="utf-8")
    index = "# Graphs\n\n" + "\n".join(f"- [{doc_spec.name}]({doc_spec.name}.md)" for doc_spec in doc_specs) + "\n"
    (out / "index.md").write_text(index, encoding="utf-8")
    return [doc_spec.name for doc_spec in doc_specs]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m harness.graphdoc")
    parser.add_argument("--out", default="docs/graphs")
    args = parser.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    names = _write_all(out, discover())
    print(f"wrote {len(names)} graph page(s) and index.md to {out}")


if __name__ == "__main__":
    main()
