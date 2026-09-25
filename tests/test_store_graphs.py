from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace as NS

import pytest

from harness.store_graphs import GraphDefinition, NodeDef, content_hash, derive_definition, register
from harness.store_migrate import open_store

NOW = "2026-09-24T00:00:00Z"
SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}}

DEFINITION = GraphDefinition(
    name="demo",
    version="1",
    nodes=(
        NodeDef("plan", "planner", "deep", "reasoning", None),
        NodeDef("build", "builder", "standard", "coding", "abc123"),
    ),
    edges=(("plan", "build"),),
)
DIGEST = "12615a215c8633cb4fe93177afdb8afcc1f58626ca10b99ba913807fa5998dbf"


def fake_graph(version="1", tier="deep"):
    return NS(
        name="demo",
        version=version,
        nodes=[
            NS(node_id="plan", role="planner", default_tier=tier, default_class="reasoning", output_schema=None),
            NS(node_id="build", role="builder", default_tier="standard", default_class="coding", output_schema=SCHEMA),
        ],
        edges=[("plan", "build")],
    )


@pytest.fixture
def conn():
    c = open_store("sqlite:///:memory:", NOW)
    yield c
    c.close()


def count(conn, table):
    return conn.query_one(f"SELECT COUNT(*) FROM {table}")[0]


def test_a_literal_definition_hashes_to_a_fixed_digest():
    assert content_hash(DEFINITION) == DIGEST


def test_the_definition_is_frozen():
    with pytest.raises(FrozenInstanceError):
        DEFINITION.name = "other"  # type: ignore[misc]


def test_changing_one_nodes_tier_changes_the_digest():
    nodes = (replace(DEFINITION.nodes[0], default_tier="fast"), DEFINITION.nodes[1])
    assert content_hash(replace(DEFINITION, nodes=nodes)) != DIGEST


def test_adding_an_edge_changes_the_digest():
    assert content_hash(replace(DEFINITION, edges=(("plan", "build"), ("build", "plan")))) != DIGEST


def test_edge_order_does_not_change_the_digest_but_node_order_does():
    two = replace(DEFINITION, edges=(("a", "b"), ("b", "c")))
    assert content_hash(two) == content_hash(replace(two, edges=(("b", "c"), ("a", "b"))))
    assert content_hash(replace(DEFINITION, nodes=DEFINITION.nodes[::-1])) != DIGEST


def test_derive_definition_keeps_declared_order_and_hashes_a_declared_schema():
    derived = derive_definition(fake_graph())
    assert [n.node_id for n in derived.nodes] == ["plan", "build"]
    assert derived.nodes[0].output_schema_hash is None
    assert derived.nodes[1].output_schema_hash == "f9daff7673f20092652712c8d445d43e9e39acd5fa49e5b5658f53037ca1b1f5"
    assert derived.edges == (("plan", "build"),)
    assert content_hash(derived) == content_hash(derive_definition(fake_graph()))


def test_registering_twice_leaves_one_graph_and_the_expected_children(conn):
    definition = derive_definition(fake_graph())
    first = register(conn, definition, NOW)
    second = register(conn, definition, "2026-09-25T00:00:00Z")
    assert first == second == content_hash(definition)
    assert (count(conn, "graphs"), count(conn, "graph_nodes"), count(conn, "graph_edges")) == (1, 2, 1)
    assert conn.query_one("SELECT registered_at, version FROM graphs") == (NOW, "1")
    assert conn.query_all("SELECT node_id, ord FROM graph_nodes ORDER BY ord") == [("plan", 0), ("build", 1)]


def test_two_versions_of_one_name_coexist(conn):
    one = register(conn, derive_definition(fake_graph(version="1")), NOW)
    two = register(conn, derive_definition(fake_graph(version="2")), NOW)
    assert one != two
    assert conn.query_all("SELECT version FROM graphs WHERE name = 'demo' ORDER BY version") == [("1",), ("2",)]
