"""Tests for the Cypher query parser."""

from __future__ import annotations

from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.cypher.parser import CypherParser


def _parse(tmp_path, name: str, src: bytes):
    p = tmp_path / name
    p.write_bytes(src)
    ctx = ParseContext(
        path=name,
        abs_path=p,
        source=src,
        repo_root=tmp_path,
    )
    return CypherParser().parse_file(ctx)


# ---------------------------------------------------------------------------
# Named queries
# ---------------------------------------------------------------------------

NAMED_QUERY = b"""// name: findPersonaByProject
MATCH (p:Persona {projectUuid: $projectUuid})
RETURN p;
"""


def test_named_query(tmp_path):
    rec = _parse(tmp_path, "queries.cypher", NAMED_QUERY)
    assert rec.language == "cypher"
    assert rec.type == "code"
    fns = rec.functions
    assert len(fns) == 1
    assert fns[0].name == "findPersonaByProject"
    assert fns[0].type == "cypher_query"


ANNOTATION_QUERY = b"""// @name createUser
CREATE (u:User {email: $email, name: $name})
RETURN u;
"""


def test_at_name_annotation(tmp_path):
    rec = _parse(tmp_path, "queries.cypher", ANNOTATION_QUERY)
    assert rec.functions[0].name == "createUser"


SQL_STYLE_COMMENT = b"""-- name: deleteUser
MATCH (u:User {id: $id})
DELETE u;
"""


def test_sql_style_name_comment(tmp_path):
    rec = _parse(tmp_path, "queries.cypher", SQL_STYLE_COMMENT)
    assert rec.functions[0].name == "deleteUser"


# ---------------------------------------------------------------------------
# Unnamed queries → generated name
# ---------------------------------------------------------------------------

UNNAMED_QUERY = b"""MATCH (n:Person) RETURN n;"""


def test_unnamed_query_gets_generated_name(tmp_path):
    rec = _parse(tmp_path, "queries.cypher", UNNAMED_QUERY)
    assert len(rec.functions) == 1
    fn = rec.functions[0]
    assert fn.name.startswith("query_")


# ---------------------------------------------------------------------------
# Node labels → Class records with type="graph_node"
# ---------------------------------------------------------------------------

LABELED_QUERY = b"""// name: findAll
MATCH (p:Persona)-[:BELONGS_TO]->(project:Project)
RETURN p, project;
"""


def test_node_labels_extracted(tmp_path):
    rec = _parse(tmp_path, "queries.cypher", LABELED_QUERY)
    graph_nodes = [c for c in rec.classes if c.type == "graph_node"]
    node_names = {c.name for c in graph_nodes}
    assert "Persona" in node_names
    assert "Project" in node_names


def test_node_class_source(tmp_path):
    rec = _parse(tmp_path, "queries.cypher", LABELED_QUERY)
    graph_nodes = [c for c in rec.classes if c.type == "graph_node"]
    for node in graph_nodes:
        assert getattr(node, "source", None) == "neo4j-cypher"


# ---------------------------------------------------------------------------
# Relationship types → Class records with type="graph_relationship"
# ---------------------------------------------------------------------------


def test_relationship_types_extracted(tmp_path):
    rec = _parse(tmp_path, "queries.cypher", LABELED_QUERY)
    rels = [c for c in rec.classes if c.type == "graph_relationship"]
    rel_names = {c.name for c in rels}
    assert "BELONGS_TO" in rel_names


REL_QUERY = b"""// name: multiRel
MATCH (a:Author)-[:WROTE]->(b:Book)-[:PUBLISHED_BY]->(pub:Publisher)
RETURN a, b, pub;
"""


def test_multiple_relationship_types(tmp_path):
    rec = _parse(tmp_path, "queries.cypher", REL_QUERY)
    rels = [c for c in rec.classes if c.type == "graph_relationship"]
    rel_names = {c.name for c in rels}
    assert "WROTE" in rel_names
    assert "PUBLISHED_BY" in rel_names


# ---------------------------------------------------------------------------
# Parameters extraction
# ---------------------------------------------------------------------------

PARAM_QUERY = b"""// name: searchUser
MATCH (u:User {email: $email})
WHERE u.name = $name
RETURN u;
"""


def test_params_extracted(tmp_path):
    rec = _parse(tmp_path, "queries.cypher", PARAM_QUERY)
    fn = rec.functions[0]
    param_names = [p.name for p in fn.params]
    assert "email" in param_names
    assert "name" in param_names


# ---------------------------------------------------------------------------
# Multiple queries in one file
# ---------------------------------------------------------------------------

MULTI_QUERY = b"""// name: getPersona
MATCH (p:Persona {uuid: $uuid})
RETURN p;

// name: listProjects
MATCH (proj:Project)
RETURN proj;
"""


def test_multiple_queries(tmp_path):
    rec = _parse(tmp_path, "queries.cypher", MULTI_QUERY)
    fn_names = [f.name for f in rec.functions]
    assert "getPersona" in fn_names
    assert "listProjects" in fn_names


def test_unique_labels_across_queries(tmp_path):
    """Same label appearing in multiple queries should only produce one Class."""
    src = b"""// name: q1
MATCH (p:Person) RETURN p;

// name: q2
MATCH (p:Person)-[:KNOWS]->(q:Person) RETURN p, q;
"""
    rec = _parse(tmp_path, "queries.cypher", src)
    person_nodes = [c for c in rec.classes if c.type == "graph_node" and c.name == "Person"]
    assert len(person_nodes) == 1


# ---------------------------------------------------------------------------
# Empty file
# ---------------------------------------------------------------------------


def test_empty_file(tmp_path):
    rec = _parse(tmp_path, "empty.cypher", b"")
    assert rec.language == "cypher"
    assert rec.functions == []
    assert rec.classes == []


def test_whitespace_only_file(tmp_path):
    rec = _parse(tmp_path, "empty.cypher", b"   \n\n   ")
    assert rec.functions == []
    assert rec.classes == []


# ---------------------------------------------------------------------------
# calls[] — node label references on each Function
# ---------------------------------------------------------------------------


def test_calls_reference_node_labels(tmp_path):
    rec = _parse(tmp_path, "queries.cypher", LABELED_QUERY)
    fn = next(f for f in rec.functions if f.name == "findAll")
    call_names = {c.name for c in fn.calls}
    assert "Persona" in call_names
    assert "Project" in call_names


def test_calls_path_is_none(tmp_path):
    rec = _parse(tmp_path, "queries.cypher", LABELED_QUERY)
    fn = rec.functions[0]
    for c in fn.calls:
        assert c.path is None


def test_source_code_populated(tmp_path):
    rec = _parse(tmp_path, "queries.cypher", NAMED_QUERY)
    fn = rec.functions[0]
    source_code = getattr(fn, "sourceCode", None)
    assert source_code is not None
    assert "MATCH" in source_code
    assert "RETURN" in source_code


def test_calls_per_query_not_deduplicated_across_file(tmp_path):
    """A label seen in multiple queries appears in calls[] for each query that uses it."""
    src = b"""// name: q1
MATCH (p:Person) RETURN p;

// name: q2
MATCH (p:Person)-[:KNOWS]->(q:Person) RETURN p, q;
"""
    rec = _parse(tmp_path, "queries.cypher", src)
    q1 = next(f for f in rec.functions if f.name == "q1")
    q2 = next(f for f in rec.functions if f.name == "q2")
    assert any(c.name == "Person" for c in q1.calls)
    assert any(c.name == "Person" for c in q2.calls)


def test_unnamed_query_calls_populated(tmp_path):
    rec = _parse(tmp_path, "queries.cypher", UNNAMED_QUERY)
    fn = rec.functions[0]
    assert any(c.name == "Person" for c in fn.calls)
