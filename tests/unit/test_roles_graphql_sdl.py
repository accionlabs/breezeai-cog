"""Role table: docs/entry-point-roles/graphql-sdl.md."""

from __future__ import annotations

from breezeai_cog.parsers.contract import is_entry_point

EP = {"route", "eventbus_consumer", "timer"}


def _eps(stmts):
    return [s for s in stmts if is_entry_point(s)]


def test_root_fields_are_captured_but_not_entry_points(analyze_files) -> None:
    # The resolver is the entry point; a root field only declares the operation (Q1).
    st = analyze_files(
        {
            "schema.graphql": (
                "type Query {\n  book(id: ID!): Book\n}\n"
                "extend type Mutation {\n  addBook(title: String!): Book\n}\n"
                "type Subscription {\n  bookAdded: Book\n}\n"
            )
        }
    )
    assert _eps(st) == []
    assert {"book", "addBook", "bookAdded"} <= {s.name for s in st if s.semanticType is None}


def test_schema_block_is_wiring(analyze_files) -> None:
    st = analyze_files(
        {
            "schema.graphql": (
                "schema { query: RootQuery }\ntype RootQuery {\n  me: User\n}\ntype User { name: String }\n"
            )
        }
    )
    assert _eps(st) == []
    assert [s.startLine for s in st if s.name == "me"] == [3]


def test_data_type_field_is_none(analyze_files) -> None:
    st = analyze_files({"schema.graphql": "type Book {\n  title: String\n  author: Author\n}\n"})
    assert _eps(st) == []


def test_key_entity_is_not_an_entry_point(analyze_files) -> None:
    st = analyze_files({"schema.graphql": 'type Book @key(fields: "id") {\n  id: ID!\n}\n'})
    assert _eps(st) == []
    assert [s.endpoint for s in st if s.semanticType == "graphql_entity"] == ["Book"]


def test_operation_document_is_outbound(analyze_files) -> None:
    st = analyze_files({"ops.graphql": "query GetBook {\n  book(id: 1) { id }\n}\n"})
    assert _eps(st) == []
    assert [(s.method, s.endpoint) for s in st if s.semanticType == "api_call"] == [
        ("QUERY", "book")
    ]


def test_type_system_is_none(analyze_files) -> None:
    st = analyze_files(
        {
            "schema.graphql": (
                "scalar Date\nenum Color { RED }\ninput BookInput { title: String }\n"
                "directive @auth on FIELD_DEFINITION\n"
            )
        }
    )
    assert _eps(st) == []
