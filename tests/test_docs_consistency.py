"""The two graph tables must agree, and both must match the tree.

README.md and docs/GRAPH-CONTRACT.md each carry a table of the graphs and their
node sequences. They drifted: when #8 added three optional roles between plan
and build, GRAPH-CONTRACT.md was updated and README.md was not, and the stale
row sat on main undetected. CI cannot catch that on its own — the workflow has
`paths-ignore: ['**.md', ...]`, so a docs-only change runs nothing. The drift
is therefore only catchable when CODE changes, which is exactly when it is
introduced. This module makes the next code change the moment it is caught.

Three things are pinned:

  1. The two tables list the same set of graph names.
  2. For each graph, the node sequence matches — the first sentence of the
     Shape cell, the arrow chain up to the first period. Only that. The two
     documents deliberately gloss differently after the sequence ("An idea
     into phases and a task DAG" vs "Idea into phases and a task DAG."), and a
     check that demanded identical prose would be brittle and would fail today.
  3. Every module under graphs/ that declares a top-level `SPEC` has a row in
     both tables, and neither table lists a graph that has no such module.
     This is what catches a new graph added with no doc row at all.

A failure names the drifting graph and shows both sequences. "assert False"
would send the next person to read two markdown tables by hand.

    pytest tests/test_docs_consistency.py -q
"""

from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GRAPHS_DIR = ROOT / "graphs"
README = ROOT / "README.md"
CONTRACT = ROOT / "docs" / "GRAPH-CONTRACT.md"

# The table is found by its header row, not by a heading or a line number:
# both documents title the section "## The graphs" today, but a heading is
# prose and prose moves. A row whose first cell is literally `Graph` is the
# table itself.
TABLE_HEADER = re.compile(r"^\|\s*Graph\s*\|")
SEPARATOR_ROW = re.compile(r"^\|\s*:?-+")


def _parse_graph_table(path: Path) -> dict[str, str]:
    """Map graph name -> Shape cell for the one graph table in `path`.

    Column count is not assumed: README has three (Graph, Namespace, Shape)
    and GRAPH-CONTRACT has two (Graph, Shape). The name is the first cell with
    its backticks stripped; the shape is whichever column the header calls
    `Shape`, so a column added later does not silently shift the comparison.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    starts = [i for i, line in enumerate(lines) if TABLE_HEADER.match(line)]
    assert len(starts) == 1, (
        f"{path.name}: expected exactly one table whose first header cell is `Graph`, "
        f"found {len(starts)} (at lines {[s + 1 for s in starts]})"
    )
    header = _cells(lines[starts[0]])
    assert "Shape" in header, f"{path.name}: the graph table has no `Shape` column: {header}"
    shape_col = header.index("Shape")

    rows: dict[str, str] = {}
    for line in lines[starts[0] + 1 :]:
        if SEPARATOR_ROW.match(line):
            continue
        if not line.startswith("|"):
            break  # the table ended
        cells = _cells(line)
        assert len(cells) == len(header), (
            f"{path.name}: row has {len(cells)} cells, header has {len(header)}:\n    {line}"
        )
        name = cells[0].strip("`")
        assert name not in rows, f"{path.name}: `{name}` appears twice in the graph table"
        rows[name] = cells[shape_col]
    assert rows, f"{path.name}: the graph table has a header and no rows"
    return rows


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _node_sequence(shape: str) -> str:
    """The first sentence of a Shape cell: the arrow chain, up to the first period.

    A cell with no period at all is a bare sequence and is taken whole. This
    is why a trailing gloss ("One task.") is free to differ between the two
    documents while the chain before it is not.
    """
    head, _, _ = shape.partition(".")
    return " ".join(head.split())


def _spec_graph_names() -> set[str]:
    """Every graph under graphs/ that declares a top-level SPEC, by its graph_name.

    Read from the tree, not from a list here: a list here is a third table
    to keep in sync, which is the disease this module treats.
    """
    sys.path.insert(0, str(ROOT))
    names: set[str] = set()
    for path in sorted(GRAPHS_DIR.rglob("*.py")):
        if path.stem.startswith("__"):
            continue
        dotted = ".".join(("graphs", *path.relative_to(GRAPHS_DIR).with_suffix("").parts))
        module = importlib.import_module(dotted)
        spec = getattr(module, "SPEC", None)
        if spec is not None:
            names.add(spec.graph_name)
    return names


@pytest.fixture(scope="module")
def readme_table() -> dict[str, str]:
    return _parse_graph_table(README)


@pytest.fixture(scope="module")
def contract_table() -> dict[str, str]:
    return _parse_graph_table(CONTRACT)


def test_both_tables_list_the_same_graphs(readme_table, contract_table) -> None:
    only_readme = sorted(set(readme_table) - set(contract_table))
    only_contract = sorted(set(contract_table) - set(readme_table))
    assert not only_readme and not only_contract, (
        "README.md and docs/GRAPH-CONTRACT.md list different graphs.\n"
        f"  only in README.md:              {only_readme}\n"
        f"  only in docs/GRAPH-CONTRACT.md: {only_contract}"
    )


def test_each_graph_has_the_same_node_sequence_in_both_tables(readme_table, contract_table) -> None:
    """The chain before the first period, and nothing after it."""
    drift = [
        f"  {name}\n"
        f"    README.md:              {_node_sequence(readme_table[name])}\n"
        f"    docs/GRAPH-CONTRACT.md: {_node_sequence(contract_table[name])}"
        for name in sorted(set(readme_table) & set(contract_table))
        if _node_sequence(readme_table[name]) != _node_sequence(contract_table[name])
    ]
    assert not drift, (
        "The node sequence differs between the two graph tables. One of them was "
        "updated when the graph changed and the other was not:\n" + "\n".join(drift)
    )


def test_every_spec_has_a_row_in_both_tables_and_no_row_lacks_a_spec(readme_table, contract_table) -> None:
    """The tables against the tree: nothing undocumented, nothing phantom."""
    specs = _spec_graph_names()
    assert specs, "no module under graphs/ declares a SPEC — the check would pass by finding nothing"
    problems = []
    for doc, table in (("README.md", readme_table), ("docs/GRAPH-CONTRACT.md", contract_table)):
        undocumented = sorted(specs - set(table))
        phantom = sorted(set(table) - specs)
        if undocumented:
            problems.append(f"  {doc} has no row for a graph that declares a SPEC: {undocumented}")
        if phantom:
            problems.append(f"  {doc} lists a graph no module under graphs/ declares a SPEC for: {phantom}")
    assert not problems, (
        "The graph tables and the modules under graphs/ disagree:\n" + "\n".join(problems)
    )


def test_node_sequence_is_the_first_sentence_only() -> None:
    """Pin the rule the comparison rests on, so a change to it is deliberate."""
    assert _node_sequence("a → b → c. Trailing gloss, ignored.") == "a → b → c"
    assert _node_sequence("a → b → c") == "a → b → c"
    assert _node_sequence("a  →   b") == "a → b", "whitespace runs must not count as drift"
    assert _node_sequence("a → b. One task.") == _node_sequence("a → b. An idea into phases.")
