"""GraphQL framework parser: resolver-map + SDL route detection, type-resolver
exclusion, DTO capture, base reuse, and per-file selection."""

from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from breezeai_cog.core import registry
from breezeai_cog.emit import to_line
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.typescript_graphql.parser import GraphQLParser
from breezeai_cog.schemas import FileRecord

# Resolver map (Apollo / graphql-tools). Root operations under Query/Mutation are routes;
# the ProcurementItem type-field resolver is NOT a route.
RESOLVER_SRC = b'''import type { Resolvers } from './generated.js';

export const resolvers: Resolvers = {
  Query: {
    procurementItem: async (_, { id }, ctx) => ctx.repo.byId(id),
    procurementItems: (_, args, ctx) => ctx.repo.list(args),
  },
  Mutation: {
    createProcurementItem: (_, { input }, ctx) => ctx.repo.create(input),
  },
  ProcurementItem: {
    tenders: (parent, _, ctx) => ctx.repo.tendersFor(parent.id),
  },
};
'''

# SDL in a gql template — carries request/response DTOs.
SDL_SRC = b'''import gql from 'graphql-tag';

export const typeDefs = gql`
  type Query {
    procurementItem(id: ID!): ProcurementItem
    procurementItems(
      filter: ProcurementItemFilter
      pagination: PaginationInput
    ): ProcurementItemConnection!
  }
  type Mutation {
    createProcurementItem(input: CreateProcurementItemInput!): ProcurementItem!
    _byIds(ids: [ID!]!): [ProcurementItem]! @merge(keyField: "id")
  }
`;
'''


# Client-side operations — a gql tagged template holding query/mutation *operations*
# (caller side), invoked via apollo.query. Mixes: named query with a variable + fragment
# spread, a mutation, and an interpolated (${...}) document. This file is NOT a resolver map
# or server SDL, so it is owned by the base TypeScriptParser, not GraphQLParser.
CLIENT_SRC = b'''import { gql } from 'apollo-angular';

const GetSpecification = gql`
  query specification($id: String!) {
    specification(id: $id) { ...SpecificationInfo }
  }
  ${fragments.specificationInfo}
`;

const CreateSpec = gql`
  mutation createSpecification($input: CreateSpecInput!) {
    createSpecification(input: $input) { id }
  }
`;

const Interpolated = gql`
  query items {
    items { ${prefixer.moved} name }
  }
`;
'''


def _parse(tmp_path, src: bytes, name: str, *, capture=True) -> FileRecord:
    p = tmp_path / name
    p.write_bytes(src)
    ctx = ParseContext(path=name, abs_path=p, source=src, repo_root=tmp_path,
                       capture_statements=capture)
    return GraphQLParser().parse_file(ctx)


def _parse_base(tmp_path, src: bytes, name: str, *, capture=True) -> FileRecord:
    """Parse via the BASE TypeScriptParser (client-op detection is additive, so it must fire
    even in files the GraphQLParser does not claim)."""
    from breezeai_cog.parsers.typescript.parser import TypeScriptParser
    p = tmp_path / name
    p.write_bytes(src)
    ctx = ParseContext(path=name, abs_path=p, source=src, repo_root=tmp_path,
                       capture_statements=capture)
    return TypeScriptParser().parse_file(ctx)


def test_routes_require_capture_statements(tmp_path) -> None:
    rec = _parse(tmp_path, RESOLVER_SRC, "r.resolvers.ts", capture=False)
    assert [s for s in rec.statements if s.semanticType == "route"] == []
    assert rec.framework is None


def test_resolver_map_operations_detected(tmp_path) -> None:
    rec = _parse(tmp_path, RESOLVER_SRC, "r.resolvers.ts")
    routes = {s.endpoint: s for s in rec.statements if s.semanticType == "route"}
    # Query + Mutation operations captured; type-field resolver (tenders) excluded.
    assert set(routes) == {"procurementItem", "procurementItems", "createProcurementItem"}
    assert routes["procurementItem"].routeKind == "query"
    assert routes["createProcurementItem"].routeKind == "mutation"
    assert all(r.framework == "graphql" for r in routes.values())
    # handler = operation name, with the arrow function's line; parented to file.
    assert routes["procurementItem"].handler == "procurementItem"
    assert routes["procurementItem"].handlerLine is not None
    assert all(r.parentId == rec.id for r in routes.values())
    assert rec.framework == "graphql"


def test_sdl_operations_detected_with_dtos(tmp_path) -> None:
    rec = _parse(tmp_path, SDL_SRC, "schema.ts")
    routes = {s.endpoint: s for s in rec.statements if s.semanticType == "route"}
    assert set(routes) == {
        "procurementItem", "procurementItems", "createProcurementItem", "_byIds",
    }
    # response/request DTOs cleaned of [ ] ! decorations.
    assert routes["procurementItem"].responseDTO == "ProcurementItem"
    assert routes["procurementItem"].requestDTO == "ID"
    assert routes["createProcurementItem"].requestDTO == "CreateProcurementItemInput"
    # multi-line args stay one operation (not split into filter/pagination fields).
    assert routes["procurementItems"].responseDTO == "ProcurementItemConnection"
    # trailing directive stripped from the return type.
    assert routes["_byIds"].responseDTO == "ProcurementItem"
    # SDL re-parsed from a gql`` template string has no host-AST node → synthetic
    # (not the GraphQL grammar's "field_definition", not a fabricated "graphql_field").
    assert routes["procurementItem"].nodeType == "synthetic"


def test_client_operations_detected_via_base_parser(tmp_path) -> None:
    # Client-op detection is additive: a plain gql-client file (no resolver map / no server
    # SDL) is owned by the base TypeScriptParser, yet client ops must still be captured.
    rec = _parse_base(tmp_path, CLIENT_SRC, "specification-queries.ts")
    routes = {s.endpoint: s for s in rec.statements if s.semanticType == "route"}
    # endpoint = the invoked root selection field (joins to a server route), NOT the op name.
    assert set(routes) == {"specification", "createSpecification", "items"}
    assert rec.framework == "graphql"

    spec = routes["specification"]
    assert spec.routeKind == "client_query"      # client_* distinguishes caller from server route
    assert spec.method == "QUERY"
    assert spec.framework == "graphql"
    assert spec.handler == "specification"       # operation name kept as the client-side label
    assert spec.requestDTO == "String"           # from $id: String!
    assert spec.nodeType == "synthetic"
    assert spec.parentId == rec.id

    mut = routes["createSpecification"]
    assert mut.routeKind == "client_mutation"
    assert mut.method == "MUTATION"
    assert mut.requestDTO == "CreateSpecInput"

    # Interpolated (${...}) document is not dropped — the operation header still parses.
    assert routes["items"].routeKind == "client_query"


def test_client_ops_ignore_plain_template_and_server_sdl(tmp_path) -> None:
    # A plain (untagged) template literal that merely contains the word "query" is NOT a
    # GraphQL client op.
    plain = b'const msg = `query executed in ${ms}ms for query ${name}`;\n'
    rec = _parse_base(tmp_path, plain, "log.ts")
    assert [s for s in rec.statements if s.semanticType == "route"] == []
    assert rec.framework is None

    # Server SDL is handled by the SDL pass (routeKind query/mutation), never emitted twice
    # as a client op — SDL and client passes are disjoint by GraphQL node type.
    rec_sdl = _parse(tmp_path, SDL_SRC, "schema.ts")
    kinds = {s.routeKind for s in rec_sdl.statements if s.semanticType == "route"}
    assert kinds == {"query", "mutation"}  # no client_* leakage


def test_base_extraction_reused(tmp_path) -> None:
    rec = _parse(tmp_path, RESOLVER_SRC, "r.resolvers.ts")
    assert rec.language == "typescript"


def test_output_validates(tmp_path) -> None:
    for src, name in ((RESOLVER_SRC, "r.resolvers.ts"), (SDL_SRC, "schema.ts")):
        rec = _parse(tmp_path, src, name)
        errors = list(Draft202012Validator(FileRecord.model_json_schema(by_alias=True))
                      .iter_errors(json.loads(to_line(rec))))
        assert not errors, errors


def test_claims_does_not_steal_plain_or_express_files() -> None:
    registry.clear()
    from breezeai_cog.parsers.typescript.parser import TypeScriptParser

    registry.register(TypeScriptParser())
    registry.register(GraphQLParser())
    assert registry.select("r.resolvers.ts", RESOLVER_SRC).name == "typescript-graphql"
    assert registry.select("schema.ts", SDL_SRC).name == "typescript-graphql"
    # an Express router file (no GraphQL) falls to the base parser — GraphQL doesn't claim
    # it, and Express is now an additive detector, not a selecting parser.
    express_src = b"import express from 'express';\nconst r = express.Router();\nr.get('/x', h);\n"
    assert registry.select("routes.ts", express_src).name == "typescript"
    # plain TS falls back to the base parser.
    assert registry.select("x.ts", b"const x = 1;").name == "typescript"
    registry.clear()
