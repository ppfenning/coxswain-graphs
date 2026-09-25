"""Graph registry: the definition of a loaded graph, its content hash, and one-time registration.

The registry is derived data. Graph behaviour stays Python; nothing here describes a
graph that code does not already define. `graphs.graph_id` equals `graphs.content_hash`,
so registering the same definition twice writes nothing new.

unknown: the attribute names of the substrate's loaded graph object. No graph module in
this repository builds one, so `derive_definition` reads the structural `LoadedGraph`
protocol below. Point the protocol at the substrate names when the object can be imported.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from harness.store_dialect import Connection, insert_ignore, json_text

__all__ = ["GraphDefinition", "LoadedGraph", "LoadedNode", "NodeDef", "content_hash", "derive_definition", "register"]

_GRAPH_COLUMNS = ("graph_id", "name", "version", "content_hash", "registered_at", "definition_json")
_NODE_COLUMNS = ("graph_id", "node_id", "ord", "role", "default_tier", "default_class", "output_schema_hash")
_EDGE_COLUMNS = ("graph_id", "src", "dst")


class LoadedNode(Protocol):
    node_id: str
    role: str
    default_tier: str
    default_class: str
    output_schema: Mapping[str, Any] | None


class LoadedGraph(Protocol):
    name: str
    version: str
    nodes: Iterable[LoadedNode]
    edges: Iterable[tuple[str, str]]


@dataclass(frozen=True)
class NodeDef:
    node_id: str
    role: str
    default_tier: str
    default_class: str
    output_schema_hash: str | None


@dataclass(frozen=True)
class GraphDefinition:
    name: str
    version: str
    nodes: tuple[NodeDef, ...]
    edges: tuple[tuple[str, str], ...]


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _node_def(node: LoadedNode) -> NodeDef:
    schema = node.output_schema
    return NodeDef(
        node_id=node.node_id,
        role=node.role,
        default_tier=node.default_tier,
        default_class=node.default_class,
        output_schema_hash=None if schema is None else _sha256(json_text(schema)),
    )


def derive_definition(graph: LoadedGraph) -> GraphDefinition:
    """Nodes keep declared order. A node's schema hash is sha256 of its canonical JSON."""
    return GraphDefinition(
        name=graph.name,
        version=str(graph.version),
        nodes=tuple(_node_def(n) for n in graph.nodes),
        edges=tuple((src, dst) for src, dst in graph.edges),
    )


def definition_json(definition: GraphDefinition) -> str:
    """The canonical text: hashed for the graph id and stored as definition_json."""
    return json_text(
        {
            "name": definition.name,
            "version": definition.version,
            "nodes": [
                {
                    "node_id": n.node_id,
                    "role": n.role,
                    "default_tier": n.default_tier,
                    "default_class": n.default_class,
                    "output_schema_hash": n.output_schema_hash,
                }
                for n in definition.nodes
            ],
            "edges": [list(e) for e in sorted(definition.edges)],
        }
    )


def content_hash(definition: GraphDefinition) -> str:
    """Node order is significant; edge order is not."""
    return _sha256(definition_json(definition))


def register(conn: Connection, definition: GraphDefinition, now: str) -> str:
    """Insert the definition once, keyed on its hash, in one transaction. Returns the graph_id."""
    graph_id = content_hash(definition)
    dialect = conn.dialect
    with conn.transaction():
        conn.execute(
            insert_ignore(dialect, "graphs", _GRAPH_COLUMNS, ("graph_id",)),
            (graph_id, definition.name, definition.version, graph_id, now, definition_json(definition)),
        )
        conn.executemany(
            insert_ignore(dialect, "graph_nodes", _NODE_COLUMNS, ("graph_id", "node_id")),
            [
                (graph_id, n.node_id, i, n.role, n.default_tier, n.default_class, n.output_schema_hash)
                for i, n in enumerate(definition.nodes)
            ],
        )
        conn.executemany(
            insert_ignore(dialect, "graph_edges", _EDGE_COLUMNS, ("graph_id", "src", "dst")),
            [(graph_id, src, dst) for src, dst in definition.edges],
        )
    return graph_id
