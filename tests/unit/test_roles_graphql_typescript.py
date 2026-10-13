"""GraphQL in TS / JS role table (docs/entry-point-roles/graphql-typescript.md): one test per row."""

from __future__ import annotations

import pytest

EP = {"route", "eventbus_consumer", "timer"}
PKG = '{"dependencies": {"@apollo/server": "4", "graphql": "16"}}'
MAP_HEAD = "import { Resolvers } from './generated';\nexport const resolvers: Resolvers = {\n"


def _eps(stmts):
    return [s for s in stmts if s.semanticType in EP and s.routeKind != "field_resolver"]


def _run(analyze_files, files: dict[str, str]):
    return analyze_files({"package.json": PKG, **files})


def test_resolver_map_root_fields_are_bindings(analyze_files) -> None:
    st = _run(
        analyze_files,
        {
            "src/resolvers.ts": MAP_HEAD
            + "  Query: {\n    books: async (_, args, ctx) => ctx.db.books(),\n"
            "    book(_, { id }) { return null; },\n  },\n"
            "  Mutation: {\n    addBook: (_, { input }) => null,\n  },\n"
            "  Subscription: {\n    bookAdded: { subscribe: () => null },\n  },\n};\n"
        },
    )
    got = {
        (r.framework, r.method, r.routeKind, r.endpoint, r.handler, r.handlerLine) for r in _eps(st)
    }
    assert got == {
        ("graphql", "QUERY", "query", "books", "books", 4),
        ("graphql", "QUERY", "query", "book", "book", 5),
        ("graphql", "MUTATION", "mutation", "addBook", "addBook", 8),
        ("graphql", "SUBSCRIPTION", "subscription", "bookAdded", "bookAdded", 11),
    }


def test_server_registration_is_wiring(analyze_files) -> None:
    st = _run(
        analyze_files,
        {
            "src/server.ts": "import { ApolloServer } from '@apollo/server';\n"
            "import { resolvers } from './resolvers';\n"
            "import { typeDefs } from './schema';\n"
            "const server = new ApolloServer({ typeDefs, resolvers });\n"
        },
    )
    assert _eps(st) == []


def test_data_type_fields_in_a_resolver_map_are_none(analyze_files) -> None:
    st = _run(
        analyze_files,
        {
            "src/resolvers.ts": MAP_HEAD + "  Query: {\n    books: () => [],\n  },\n"
            "  Book: {\n    author: (book) => null,\n  },\n};\n"
        },
    )
    assert [r.endpoint for r in _eps(st)] == ["books"]


@pytest.mark.xfail(strict=True, reason="__resolveReference is not recorded")
def test_resolve_reference_is_a_binding(analyze_files) -> None:
    st = _run(
        analyze_files,
        {
            "src/resolvers.ts": MAP_HEAD + "  Query: {\n    books: () => [],\n  },\n"
            "  Book: {\n    __resolveReference: (ref) => null,\n  },\n};\n"
        },
    )
    got = {(r.method, r.routeKind, r.endpoint) for r in _eps(st)}
    assert ("RESOLVE_REFERENCE", "query", "Book") in got


CF = "import { Query, Mutation, FieldResolver, RequireScopes } from '../decorators';\n"


def test_code_first_operation_is_a_binding(analyze_files) -> None:
    st = _run(
        analyze_files,
        {
            "src/services/product.service.ts": CF + "export class ProductService {\n"
            "  @Query('products(input: ProductFilter!): [Product!]!')\n"
            "  async products(input) { return []; }\n"
            "  @Mutation()\n  async addProduct(input) { return null; }\n}\n"
        },
    )
    got = {(r.method, r.routeKind, r.endpoint, r.handler) for r in _eps(st)}
    assert got == {
        ("QUERY", "query", "products", "products"),
        ("MUTATION", "mutation", "addProduct", "addProduct"),
    }


def test_code_first_field_resolver_is_none(analyze_files) -> None:
    st = _run(
        analyze_files,
        {
            "src/services/product.service.ts": CF + "export class ProductService {\n"
            "  @Query()\n  async products() { return []; }\n"
            "  @FieldResolver()\n  price() { return 1; }\n}\n"
        },
    )
    assert [r.endpoint for r in _eps(st)] == ["products"]


def test_code_first_scopes_are_guards(analyze_files) -> None:
    st = _run(
        analyze_files,
        {
            "src/services/product.service.ts": CF
            + "@RequireScopes('admin')\nexport class ProductService {\n"
            "  @Query()\n  async products() { return []; }\n}\n"
        },
    )
    [r] = _eps(st)
    assert r.guards == ["RequireScopes"]


TG = "import { Resolver, Query, Mutation, FieldResolver, Root } from 'type-graphql';\n"


def test_type_graphql_operations_are_bindings(analyze_files) -> None:
    st = _run(
        analyze_files,
        {
            "src/recipe.resolver.ts": TG
            + "@Resolver(() => Recipe)\nexport class RecipeResolver {\n"
            "  @Query(() => [Recipe])\n  recipes() { return []; }\n"
            "  @Mutation(() => Recipe)\n  addRecipe() { return null; }\n}\n"
        },
    )
    got = {(r.method, r.endpoint, r.handler) for r in _eps(st)}
    assert got == {("QUERY", "recipes", "recipes"), ("MUTATION", "addRecipe", "addRecipe")}


def test_type_graphql_field_resolver_is_none(analyze_files) -> None:
    st = _run(
        analyze_files,
        {
            "src/recipe.resolver.ts": TG
            + "@Resolver(() => Recipe)\nexport class RecipeResolver {\n"
            "  @FieldResolver()\n  ratings(@Root() r: Recipe) { return []; }\n}\n"
        },
    )
    assert _eps(st) == []


def test_client_operation_is_outbound(analyze_files) -> None:
    st = _run(
        analyze_files,
        {
            "src/client.ts": "import { gql } from '@apollo/client';\n"
            "const GET = gql`query GetBooks { books { id } }`;\n"
        },
    )
    assert _eps(st) == []
    assert [(s.endpoint, s.handler) for s in st if s.semanticType == "api_call"] == [
        ("books", "GetBooks")
    ]
