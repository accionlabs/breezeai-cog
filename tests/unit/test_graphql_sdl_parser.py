"""Standalone GraphQL SDL/operation capture (BREEZEAI-528).

The `graphql` language parser owns `.graphql` / `.gql` / `.graphqls`. The type **system** is
emitted as `Class` nodes (type→class, interface→interface, enum→enum, union→union,
input→record — same as every other language); a `@key` type additionally gets a
`graphql_entity` statement with `keyFields`. Members (object/input fields, enum values, union
members) are child statements parented to their Class. Root operation fields → `route`;
client operations → `api_call`. `scalar`/`directive`/`schema` → plain statements;
descriptions → `comment`.
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

# A multi-line operation whose invoked field carries a nested selection set.
MULTILINE_OP = """\
query GetProjects($filter: ProjectFilter!, $pagination: PaginationInput!) {
  projects(filter: $filter, pagination: $pagination) {
    items { id  name }
    pageInfo { totalCount  hasNextPage }
  }
}
"""

# Custom-named roots declared via schema{} — their fields must still be routes.
SCHEMA_ROOTS = """\
schema { query: RootQuery  mutation: RootMutation }
type RootQuery { me: User }
type RootMutation { save(input: SaveInput!): User }
"""

# Extensions of non-object kinds.
EXTENDS = """\
interface Node { id: ID! }
extend interface Node { createdAt: String }
enum Role { ADMIN }
extend enum Role { GUEST }
union Search = User
extend union Search = Post
input Filter { term: String }
extend input Filter { limit: Int }
"""

DESCRIBED = '''\
"""A registered customer."""
type Customer { id: ID! }
'''


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


def _classes(rec) -> dict:
    return {c.name: c for c in rec.classes}


def _by(rec, name: str, node_type: str):
    return next(s for s in rec.statements if s.name == name and s.nodeType == node_type)


def test_language_and_extensions() -> None:
    p = GraphQLParser()
    assert p.name == "graphql"
    assert p.extensions == (".graphql", ".gql", ".graphqls")
    assert "graphql" in p.frameworks


def test_graphqls_schema_file_parsed(tmp_path) -> None:
    rec = _parse(tmp_path, "schema.graphqls", SDL)  # graphql-java / Spring extension
    assert rec.language == "graphql"
    assert _classes(rec)["User"].type == "class"
    assert _by(rec, "user", "field_definition").semanticType == "route"


def test_type_system_becomes_class_nodes(tmp_path) -> None:
    rec = _parse(tmp_path, "schema.graphql", SDL)
    cls = _classes(rec)
    assert cls["User"].type == "class"
    assert cls["Node"].type == "interface"
    assert cls["SearchResult"].type == "union"
    assert cls["Status"].type == "enum"
    assert cls["CreateUserInput"].type == "record"
    # root operation types are NOT classes
    assert "Query" not in cls and "Mutation" not in cls


def test_key_type_has_class_and_entity(tmp_path) -> None:
    rec = _parse(tmp_path, "schema.graphql", SDL)
    assert "User" in _classes(rec)  # structure
    entity = _by(rec, "User", "object_type_definition")  # federation marker
    assert entity.semanticType == "graphql_entity"
    assert entity.keyFields == ["id"]
    assert entity.parentId == _classes(rec)["User"].id  # marker is a child of the Class


def test_implements_captured(tmp_path) -> None:
    rec = _parse(tmp_path, "schema.graphql", SDL)
    assert _classes(rec)["User"].implements == ["Node"]


def test_fields_are_child_statements(tmp_path) -> None:
    rec = _parse(tmp_path, "schema.graphql", SDL)
    user_id = _classes(rec)["User"].id
    fields = [
        s for s in rec.statements if s.nodeType == "field_definition" and s.parentId == user_id
    ]
    assert {s.name for s in fields} == {"id", "name", "company", "posts", "archived"}
    # relation target survives in text (structuring it is deferred)
    assert "company: Company" in _by(rec, "company", "field_definition").text


def test_enum_values_are_child_statements(tmp_path) -> None:
    rec = _parse(tmp_path, "schema.graphql", SDL)
    status_id = _classes(rec)["Status"].id
    vals = [s for s in rec.statements if s.nodeType == "enum_value_definition"]
    assert {s.name for s in vals} == {"ACTIVE", "INACTIVE"}
    assert all(s.parentId == status_id for s in vals)


def test_union_members_are_child_statements(tmp_path) -> None:
    rec = _parse(tmp_path, "schema.graphql", SDL)
    search_id = _classes(rec)["SearchResult"].id
    members = [s for s in rec.statements if s.nodeType == "named_type" and s.parentId == search_id]
    assert {s.name for s in members} == {"User", "Company"}


def test_root_fields_are_routes(tmp_path) -> None:
    rec = _parse(tmp_path, "schema.graphql", SDL)
    q = _by(rec, "user", "field_definition")
    assert (q.semanticType, q.method, q.routeKind) == ("route", "QUERY", "query")
    assert q.responseDTO == "User"
    m = _by(rec, "createUser", "field_definition")
    assert (m.method, m.routeKind) == ("MUTATION", "mutation")
    assert m.requestDTO == "CreateUserInput"


def test_scalar_directive_are_plain_statements(tmp_path) -> None:
    rec = _parse(tmp_path, "schema.graphql", SDL)
    assert _by(rec, "DateTime", "scalar_type_definition").semanticType is None
    assert _by(rec, "auth", "directive_definition").semanticType is None


def test_schema_custom_roots_become_routes(tmp_path) -> None:
    rec = _parse(tmp_path, "s.graphql", SCHEMA_ROOTS)
    # RootQuery/RootMutation are roots (via schema{}), so NOT classes; their fields are routes
    assert "RootQuery" not in _classes(rec) and "RootMutation" not in _classes(rec)
    assert _by(rec, "me", "field_definition").semanticType == "route"
    assert _by(rec, "save", "field_definition").method == "MUTATION"


def test_extend_kinds_are_handled(tmp_path) -> None:
    rec = _parse(tmp_path, "e.graphql", EXTENDS)
    cls = _classes(rec)
    # one Class per name (def + extend merge); ClassType matches the kind
    assert cls["Node"].type == "interface"
    assert cls["Role"].type == "enum"
    assert cls["Search"].type == "union"
    assert cls["Filter"].type == "record"
    # the extension's members are attached to that same Class
    assert any(s.name == "createdAt" and s.parentId == cls["Node"].id for s in rec.statements)
    assert any(s.name == "GUEST" for s in rec.statements)  # extended enum value


def test_descriptions_captured_as_comments(tmp_path) -> None:
    rec = _parse(tmp_path, "d.graphql", DESCRIBED)
    assert _classes(rec)["Customer"].type == "class"
    descs = [s for s in rec.statements if s.semanticType == "comment"]
    assert any("A registered customer" in s.text for s in descs)


def test_operations_and_fragment(tmp_path) -> None:
    rec = _parse(tmp_path, "ops.graphql", OPERATIONS)
    get = _by(rec, "GetUser", "field")
    assert (get.semanticType, get.method, get.endpoint) == ("api_call", "QUERY", "user")
    frag = next(s for s in rec.statements if s.nodeType == "fragment_definition")
    assert frag.name == "UserFields" and frag.endpoint == "User"


def test_multiline_operation_captured_in_full(tmp_path) -> None:
    rec = _parse(tmp_path, "projects.graphql", MULTILINE_OP)
    op = _by(rec, "GetProjects", "field")
    assert op.endpoint == "projects"
    assert "items {" in op.text and "pageInfo {" in op.text
    assert op.text.rstrip().endswith("}")


def test_capture_gate(tmp_path) -> None:
    rec = _parse(tmp_path, "schema.graphql", SDL, capture=False)
    assert rec.statements == [] and rec.classes == []
    assert rec.language == "graphql"


def test_records_validate_against_schema(tmp_path) -> None:
    rec = _parse(tmp_path, "schema.graphql", SDL + "\n" + OPERATIONS)
    validator = Draft202012Validator(FileRecord.model_json_schema(by_alias=True))
    errors = list(validator.iter_errors(json.loads(to_line(rec))))
    assert not errors, errors
    assert rec.classes and rec.statements
