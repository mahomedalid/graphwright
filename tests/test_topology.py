from ddg.topology import load_topology, parse_topology


def test_example_topology_is_valid(settings):
    topology = load_topology(settings.topology_file)
    assert topology.is_valid and topology.title == "Retail Example"
    assert not topology.warnings  # YAML frontmatter is ignored
    assert set(topology.node_types) == {"Customer", "Order", "Product", "Store"}
    assert topology.edge_types["PLACED"].from_type == "Customer"
    assert topology.edge_types["CONTAINS"].properties == ["quantity", "unit price"]
    assert topology.node_types["Customer"].synonyms == ["client", "buyer"]
    assert topology.hints and topology.constraints


def test_invalid_topology_reports_actionable_errors():
    text = """# Graph Topology: Broken
## Node Types
### Thing
A thing.
- Key: id
### Thing
## Edge Types
### LINKS
- From: Thing
- To: Missing
- Cardinality: lots
### HALF
- From: Thing
"""
    topology = parse_topology(text)
    messages = " | ".join(i.message for i in topology.errors)
    assert not topology.is_valid
    assert "Duplicate type name 'Thing'" in messages
    assert "undefined node type 'Missing'" in messages
    assert "invalid cardinality 'lots'" in messages
    assert "'HALF' must define both" in messages


def test_empty_topology_and_warnings():
    assert not parse_topology("# Nothing here").is_valid
    topology = parse_topology("## Node Types\n### Widget\n- Colour: red\n")
    assert topology.is_valid
    warnings = " | ".join(i.message for i in topology.warnings)
    assert "no description" in warnings and "no '- Key:'" in warnings and "Unknown field 'colour'" in warnings


def test_version_changes_with_content():
    a = parse_topology("## Node Types\n### A\nDesc\n- Key: id\n")
    b = parse_topology("## Node Types\n### A\nOther\n- Key: id\n")
    assert a.version != b.version
