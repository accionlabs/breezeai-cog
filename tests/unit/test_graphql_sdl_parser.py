"""Standalone GraphQL SDL/operation capture (BREEZEAI-528).

The ``graphql`` language parser owns ``.graphql`` / ``.gql`` files (distinct from the
``typescript-graphql`` framework parser, which handles ``gql`…`` embedded in ``.ts``). It
emits one flat ``Statement`` per construct — composite types as ``graphql_entity`` (with the
full body, including any ``@key`` directive, on ``text``), root-type fields as ``route``,
value types (enum/input/scalar/directive/schema) as plain statements, client operations as
``api_call`` per invoked field, and fragments — each carrying its declared ``name``.
"""

from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from breezeai_cog.emit import to_line
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.graphql.parser import GraphQLParser
from breezeai_cog.schemas import FileRecord

SDL = """\
# a header comment
scalar DateTime

directive @auth(role: String!) on FIELD_DEFINITION

interface Node { id: ID! }

union SearchResult = User | Company

enum Status { ACTIVE INACTIVE }

input CreateUserInput { name: String!  email: String! }

type User implements Node @key(fields: "id") {
  id: ID!
  name: String!
  company: Company
  posts: [Post!]!
}

extend type User { archived: Boolean }

type Query {
  user(id: ID!): User
  search(term: String!): [SearchResult!]!
}

type Mutation {
  createUser(input: CreateUserInput!): User @auth(role: "admin")
}
"""

OPERATIONS = """\
query GetUser($id: ID!) { user(id: $id) { ...UserFields } }
mutation Make($input: CreateUserInput!) { createUser(input: $input) { id } }
fragment UserFields on User { id name }
"""


def _parse(tmp_path, filename: str, src: str, *, capture: bool = True) -> FileRecord:
    p = tmp_path / filename
    p.write_text(src)
    ctx = ParseContext(
        path=filename,
        abs_path=p,
        source=src.encode(),
        repo_root=tmp_path,
        capture_statements=capture,
        statement_text_limit=1000,
    )
    return GraphQLParser().parse_file(ctx)


def _by_name(rec, name: str, node_type: str):
    return next(s for s in rec.statements if s.name == name and s.nodeType == node_type)


def test_language_and_extensions() -> None:
    p = GraphQLParser()
    assert p.name == "graphql"
    assert p.extensions == (".graphql", ".gql")
    assert "graphql" in p.frameworks


def test_entity_captured_with_full_body(tmp_path) -> None:
    rec = _parse(tmp_path, "schema.graphql", SDL)
    assert rec.language == "graphql"
    user = _by_name(rec, "User", "object_type_definition")
    assert user.semanticType == "graphql_entity"
    assert user.endpoint == "User"
    # keyFields is dropped; the @key directive stays visible in the full-body text
    assert user.keyFields is None
    assert "@key" in user.text
    # full body carries columns AND relations as text (Tier-1 contents capture)
    assert "company: Company" in user.text
    assert "posts: [Post!]!" in user.text


def test_interface_union_are_entities(tmp_path) -> None:
    rec = _parse(tmp_path, "schema.gql", SDL)
    assert _by_name(rec, "Node", "interface_type_definition").semanticType == "graphql_entity"
    assert _by_name(rec, "SearchResult", "union_type_definition").semanticType == "graphql_entity"


def test_type_extension_is_entity(tmp_path) -> None:
    rec = _parse(tmp_path, "schema.graphql", SDL)
    ext = _by_name(rec, "User", "object_type_extension")
    assert ext.semanticType == "graphql_entity"
    assert ext.endpoint == "User"


def test_value_types_have_no_entity_marker(tmp_path) -> None:
    rec = _parse(tmp_path, "schema.graphql", SDL)
    for name, nt in [
        ("DateTime", "scalar_type_definition"),
        ("auth", "directive_definition"),
        ("Status", "enum_type_definition"),
        ("CreateUserInput", "input_object_type_definition"),
    ]:
        s = _by_name(rec, name, nt)
        assert s.semanticType is None
        assert s.endpoint == name  # name is the join key (e.g. requestDTO -> input)


def test_root_type_fields_become_routes(tmp_path) -> None:
    rec = _parse(tmp_path, "schema.graphql", SDL)
    q = _by_name(rec, "user", "field_definition")
    assert q.semanticType == "route"
    assert (q.method, q.routeKind, q.endpoint) == ("QUERY", "query", "user")
    assert q.responseDTO == "User"
    m = _by_name(rec, "createUser", "field_definition")
    assert (m.method, m.routeKind) == ("MUTATION", "mutation")
    assert m.requestDTO == "CreateUserInput"
    assert m.responseDTO == "User"
    # no entity emitted for the root operation types themselves
    assert not any(
        s.name in ("Query", "Mutation") and s.semanticType == "graphql_entity"
        for s in rec.statements
    )


def test_operations_and_fragment(tmp_path) -> None:
    rec = _parse(tmp_path, "ops.graphql", OPERATIONS)
    get = _by_name(rec, "GetUser", "field")
    assert get.semanticType == "api_call"
    assert (get.method, get.endpoint) == ("QUERY", "user")  # invoked server field
    frag = next(s for s in rec.statements if s.nodeType == "fragment_definition")
    assert frag.name == "UserFields"
    assert frag.endpoint == "User"


def test_capture_gate(tmp_path) -> None:
    rec = _parse(tmp_path, "schema.graphql", SDL, capture=False)
    assert rec.statements == []
    assert rec.language == "graphql"


def test_records_validate_against_schema(tmp_path) -> None:
    rec = _parse(tmp_path, "schema.graphql", SDL + "\n" + OPERATIONS)
    validator = Draft202012Validator(FileRecord.model_json_schema(by_alias=True))
    errors = list(validator.iter_errors(json.loads(to_line(rec))))
    assert not errors, errors
    assert rec.statements  # non-empty
