"""Diagrams are generated from the specs; this pins that they stay generated.

    pytest tests/test_graphdoc.py -q

Two unit tests pin the rendering rules on a literal, two-node spec. The third
is the freshness test: it regenerates every committed page in memory from the
graphs actually on the tree and asserts byte-for-byte equality, so a graph
whose docstring changes without a regen fails here, in CI, with a message
that names the fix.
"""

from __future__ import annotations

from pathlib import Path

from harness.graphdoc import Spec, for_graph, mermaid, page
from harness.registry import discover

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs" / "graphs"

TWO_NODE = Spec(
    name="demo",
    doc="""demo — a two-node graph, for the test only.

    alpha -> beta

Prose that mermaid() never looks at.
""",
)


def test_mermaid_of_a_two_node_spec_has_both_nodes_and_the_arrow() -> None:
    diagram = mermaid(TWO_NODE)
    assert "alpha" in diagram
    assert "beta" in diagram
    assert "n0 --> n1" in diagram


def test_page_has_the_heading_and_the_mermaid_fence() -> None:
    text = page(TWO_NODE)
    assert text.startswith("# demo\n")
    assert "```mermaid\n" in text
    assert "```\n" in text


def test_every_committed_page_matches_what_the_specs_regenerate_now() -> None:
    """A spec change without a regen fails here: run python -m harness.graphdoc."""
    specs = discover()
    graph_specs = sorted(specs.values(), key=lambda spec: spec.graph_name)
    stale = [
        graph_spec.graph_name
        for graph_spec in graph_specs
        if page(for_graph(graph_spec))
        != (DOCS / f"{graph_spec.graph_name}.md").read_text(encoding="utf-8")
    ]
    assert not stale, f"stale graph page(s), run python -m harness.graphdoc: {stale}"

    expected_index = (
        "# Graphs\n\n"
        + "\n".join(f"- [{graph_spec.graph_name}]({graph_spec.graph_name}.md)" for graph_spec in graph_specs)
        + "\n"
    )
    actual_index = (DOCS / "index.md").read_text(encoding="utf-8")
    assert actual_index == expected_index, "docs/graphs/index.md is stale, run python -m harness.graphdoc"
