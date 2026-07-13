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


def _parse(tmp_path, src: bytes, name: str, *, capture=True) -> FileRecord:
    p = tmp_path / name
    p.write_bytes(src)
    ctx = ParseContext(path=name, abs_path=p, source=src, repo_root=tmp_path,
                       capture_statements=capture)
    return GraphQLParser().parse_file(ctx)


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
