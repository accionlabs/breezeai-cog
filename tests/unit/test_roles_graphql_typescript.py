"""GraphQL in TS / JS role table (docs/entry-point-roles/graphql-typescript.md): one test per row."""

from __future__ import annotations

import pytest

from breezeai_cog.parsers.contract import is_entry_point


EP = {"route", "eventbus_consumer", "timer"}
PKG = '{"dependencies": {"@apollo/server": "4", "graphql": "16"}}'
MAP_HEAD = "import { Resolvers } from './generated';\nexport const resolvers: Resolvers = {\n"


def _eps(stmts):
    return [s for s in stmts if is_entry_point(s)]


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
        ("graphql", "QUERY", "query", "books", None, 4),  # inline arrow: anonymous
        ("graphql", "QUERY", "query", "book", "book", 5),  # shorthand method
        ("graphql", "MUTATION", "mutation", "addBook", None, 8),
        ("graphql", "SUBSCRIPTION", "subscription", "bookAdded", None, 11),  # its subscribe fn
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


def test_resolver_takes_its_dtos_from_a_graphql_file(analyze_files) -> None:
    # Schema-first: the SDL (a .graphql file, another parser's) declares the DTOs; the resolver
    # map is the binding and carries them.
    st = _run(
        analyze_files,
        {
            "src/schema.graphql": "type Query { book(id: ID!): Book }\n"
            "type Mutation { addBook(input: BookInput!): Book }\n",
            "src/resolvers.ts": MAP_HEAD + "  Query: {\n    book: () => null,\n  },\n"
            "  Mutation: {\n    addBook: () => null,\n  },\n};\n",
        },
    )
    got = {(r.endpoint, r.requestDTO, r.responseDTO) for r in _eps(st) if r.path == "src/resolvers.ts"}
    assert got == {("book", "ID!", "Book"), ("addBook", "BookInput!", "Book")}


def test_resolver_takes_its_dtos_from_embedded_sdl(analyze_files) -> None:
    st = _run(
        analyze_files,
        {
            "src/schema.ts": "import gql from 'graphql-tag';\n"
            "export const typeDefs = gql`\n  type Query { books(filter: BookFilter): [Book] }\n`;\n",
            "src/resolvers.ts": MAP_HEAD + "  Query: {\n    books: () => [],\n  },\n};\n",
        },
    )
    [r] = [r for r in _eps(st) if r.path == "src/resolvers.ts"]
    assert (r.requestDTO, r.responseDTO) == ("BookFilter", "[Book]")


def test_conflicting_sdl_leaves_the_dtos_empty(analyze_files) -> None:
    st = _run(
        analyze_files,
        {
            "a.graphql": "type Query { book(id: ID!): Book }\n",
            "b.graphql": "type Query { book(id: ID!): BookV2 }\n",
            "src/resolvers.ts": MAP_HEAD + "  Query: {\n    book: () => null,\n  },\n};\n",
        },
    )
    [r] = [r for r in _eps(st) if r.path == "src/resolvers.ts"]
    assert (r.requestDTO, r.responseDTO) == (None, None)


SERVER_NS = (
    "import { ApolloServer } from '@apollo/server';\n"
    "import * as queries from './resolvers/queries';\n"
    "import * as mutations from './resolvers/mutations';\n"
    "const resolvers = {\n"
    "  Mutation: mutations,\n"
    "  Query: queries,\n"
    "};\n"
    "export const server = new ApolloServer({ typeDefs, resolvers });\n"
)


def test_resolver_map_root_from_a_namespace_import(analyze_files) -> None:
    # `Query: queries`: every name the module exports is one operation — through a barrel's
    # `default as` re-export, a direct `export const`, and an `export *`.
    st = _run(
        analyze_files,
        {
            "src/server.ts": SERVER_NS,
            "src/resolvers/queries/index.ts":
                "export { default as dummyQuery } from './dummyQuery';\n"
                "export * from './more';\n",
            "src/resolvers/queries/dummyQuery.ts":
                "async function dummyQuery(_: any, args: any) { return null; }\n"
                "export default dummyQuery;\n",
            "src/resolvers/queries/more.ts":
                "export const books = async () => [];\n",
            "src/resolvers/mutations/index.ts":
                "export async function addBook(_: any, { input }: any) { return null; }\n",
        },
    )
    got = {
        (r.id.split(":", 1)[1], r.method, r.routeKind, r.endpoint, r.handler, r.handlerPath,
         r.handlerLine)
        for r in _eps(st)
    }
    # One record per operation on the root key, leading to the function in its own file.
    assert got == {
        ("5:2", "MUTATION", "mutation", "addBook", "addBook", "src/resolvers/mutations/index.ts", 1),
        ("6:2", "QUERY", "query", "dummyQuery", "dummyQuery",
         "src/resolvers/queries/dummyQuery.ts", 1),
        ("6:2#2", "QUERY", "query", "books", "books", "src/resolvers/queries/more.ts", 1),
    }


def test_resolver_map_root_from_a_missing_module_is_none(analyze_files) -> None:
    # The imported module isn't in the repo: no operations are invented.
    st = _run(analyze_files, {"src/server.ts": SERVER_NS})
    assert _eps(st) == []


SERVER_NAMED = (
    "import { ApolloServer } from '@apollo/server';\n"
    "import { queries } from './queries';\n"
    "const resolvers = { Query: queries };\n"
    "export const server = new ApolloServer({ typeDefs, resolvers });\n"
)


def test_resolver_map_root_from_a_default_import_is_none(analyze_files) -> None:
    # Only a namespace or a named import names a module's operations; a default import does not.
    st = _run(
        analyze_files,
        {
            "src/server.ts": "import queries from './queries';\nconst resolvers = { Query: queries };\n",
            "src/queries.ts": "export default { books: async () => [] };\n",
        },
    )
    assert _eps(st) == []


def test_resolver_map_root_from_a_default_exported_const_is_none(analyze_files) -> None:
    # `export default queries` names a declaration, but a default import is still not read.
    st = _run(
        analyze_files,
        {
            "src/server.ts": "import queries from './queries';\nconst resolvers = { Query: queries };\n",
            "src/queries.ts": "export const queries = { books: async () => [] };\nexport default queries;\n",
        },
    )
    assert _eps(st) == []


def test_resolver_map_root_from_a_local_object_is_none(analyze_files) -> None:
    # An object declared in the same file is not an import; no operations are read from it.
    st = _run(
        analyze_files,
        {"src/server.ts": "const queries = { books: async () => [] };\n"
                          "const resolvers = { Query: queries };\n"},
    )
    assert _eps(st) == []


def test_resolver_map_root_from_a_missing_named_import_is_none(analyze_files) -> None:
    # The named import's module isn't in the repo: no operations are invented.
    st = _run(analyze_files, {"src/server.ts": SERVER_NAMED})
    assert _eps(st) == []


SERVER_NAMED_TWO = (
    "import { ApolloServer } from '@apollo/server';\n"
    "import { queries } from './queries';\n"
    "import { mutations as muts } from './mutations';\n"
    "const resolvers = {\n"
    "  Mutation: muts,\n"
    "  Query: queries,\n"
    "};\n"
    "export const server = new ApolloServer({ typeDefs, resolvers });\n"
)


def _ops(st, path: str | None = None):
    return {
        (r.id.split(":", 1)[1], r.method, r.routeKind, r.endpoint, r.handler, r.handlerPath,
         r.handlerLine)
        for r in _eps(st)
        if path is None or r.path == path
    }


def test_resolver_map_root_from_a_named_import(analyze_files) -> None:
    # `Query: queries`, `queries` an object of functions in another file: one operation per
    # property, leading to where its function starts. An inline function has no name; a shorthand
    # method does; `books: getAuthors` is not an inline function, so nothing is claimed for it.
    st = _run(
        analyze_files,
        {
            "src/server.ts": SERVER_NAMED_TWO,
            "src/queries.ts":
                "export const queries = {\n"
                "  books: async () => [],\n"
                "  book(_: any, { id }: any) { return null; },\n"
                "  authors: getAuthors,\n"
                "};\n"
                "function getAuthors() { return []; }\n",
            "src/mutations.ts":
                "export const mutations = {\n"
                "  addBook: function (_: any, { input }: any) { return null; },\n"
                "};\n",
        },
    )
    assert _ops(st) == {
        ("5:2", "MUTATION", "mutation", "addBook", None, "src/mutations.ts", 2),
        ("6:2", "QUERY", "query", "books", None, "src/queries.ts", 2),
        ("6:2#2", "QUERY", "query", "book", "book", "src/queries.ts", 3),
        ("6:2#3", "QUERY", "query", "authors", None, None, None),
    }


def test_resolver_map_root_from_a_subscription_object(analyze_files) -> None:
    st = _run(
        analyze_files,
        {
            "src/server.ts": "import { subs } from './subs';\nconst resolvers = { Subscription: subs };\n",
            "src/subs.ts": "export const subs = {\n  bookAdded: { subscribe: () => null },\n};\n",
        },
    )
    assert _ops(st) == {
        ("2:20", "SUBSCRIPTION", "subscription", "bookAdded", None, "src/subs.ts", 2),
    }


def test_resolver_map_root_from_a_renamed_and_reexported_named_import(analyze_files) -> None:
    # `import { queries as q }` binds the local name; the export is followed through a barrel.
    st = _run(
        analyze_files,
        {
            "src/server.ts": "import { queries as q } from './resolvers';\nconst resolvers = { Query: q };\n",
            "src/resolvers/index.ts": "export { queries } from './queries';\n",
            "src/resolvers/queries.ts": "export const queries = {\n  books: () => [],\n};\n",
        },
    )
    assert _ops(st) == {
        ("2:20", "QUERY", "query", "books", None, "src/resolvers/queries.ts", 2),
    }


VITE_ALIAS = (
    "import { fileURLToPath, URL } from 'node:url';\n"
    "export default { resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } } };\n"
)


def test_resolver_map_root_from_a_named_import_through_an_alias(analyze_files) -> None:
    # The import is written through a Vite `resolve.alias` entry; it leads to the same file.
    st = _run(
        analyze_files,
        {
            "vite.config.js": VITE_ALIAS,
            "src/server.ts": "import { queries } from '@/queries';\nconst resolvers = { Query: queries };\n",
            "src/queries.ts": "export const queries = {\n  books: () => [],\n};\n",
        },
    )
    assert _ops(st) == {("2:20", "QUERY", "query", "books", None, "src/queries.ts", 2)}


def test_named_import_root_skips_spreads_and_computed_keys(analyze_files) -> None:
    st = _run(
        analyze_files,
        {
            "src/server.ts": SERVER_NAMED,
            "src/queries.ts": "export const queries = {\n  ...base,\n  books: () => [],\n"
                              "  [dynamic]: () => [],\n};\n",
        },
    )
    assert _ops(st) == {("3:20", "QUERY", "query", "books", None, "src/queries.ts", 3)}


def test_named_import_root_takes_its_dtos_from_a_graphql_file(analyze_files) -> None:
    st = _run(
        analyze_files,
        {
            "src/schema.graphql": "type Query { book(id: ID!): Book }\n",
            "src/server.ts": SERVER_NAMED,
            "src/queries.ts": "export const queries = {\n  book: () => null,\n};\n",
        },
    )
    got = {(r.endpoint, r.requestDTO, r.responseDTO) for r in _eps(st) if r.path == "src/server.ts"}
    assert got == {("book", "ID!", "Book")}


@pytest.mark.parametrize("queries_ts", [
    "export const queries = { limit: 10, books: async () => [] };\n",   # a data property
    "export const queries = { books: async () => [], tags: ['a'] };\n",  # an array property
    "export const queries = { books: async () => [], page: { size: 1 } };\n",  # a plain object
    "export const queries = makeQueries();\n",                         # not an object literal
    "export const queries = {};\n",                                    # nothing to expose
    "const queries = { books: async () => [] };\n",                    # not exported
    "export default { books: async () => [] };\n",                     # not a named export
])
def test_named_import_root_that_is_not_a_resolver_object_is_none(analyze_files, queries_ts) -> None:
    st = _run(analyze_files, {"src/server.ts": SERVER_NAMED, "src/queries.ts": queries_ts})
    assert _eps(st) == []


def test_named_import_root_from_a_package_is_none(analyze_files) -> None:
    st = _run(
        analyze_files,
        {"src/server.ts": SERVER_NAMED.replace("'./queries'", "'some-package'")},
    )
    assert _eps(st) == []


def test_resolver_map_root_written_shorthand(analyze_files) -> None:
    # `{ Query, Mutation }` after `import { Query } from './Query'`: the key is the local name.
    st = _run(
        analyze_files,
        {
            "src/server.ts": "import { Query } from './Query';\nimport { Mutation } from './Mutation';\n"
                             "export const resolvers = {\n  Query,\n  Mutation,\n};\n",
            "src/Query.ts": "export const Query = {\n  books: () => [],\n};\n",
            "src/Mutation.ts": "export const Mutation = {\n  addBook: () => null,\n};\n",
        },
    )
    assert _ops(st) == {
        ("4:2", "QUERY", "query", "books", None, "src/Query.ts", 2),
        ("5:2", "MUTATION", "mutation", "addBook", None, "src/Mutation.ts", 2),
    }


def test_resolver_map_root_written_shorthand_from_a_namespace_import(analyze_files) -> None:
    st = _run(
        analyze_files,
        {
            "src/server.ts": "import * as Query from './Query';\nexport const resolvers = {\n  Query,\n};\n",
            "src/Query.ts": "export const books = async () => [];\n",
        },
    )
    assert _ops(st) == {("3:2", "QUERY", "query", "books", "books", "src/Query.ts", 1)}


QUERIES_TS = "export const queries = {\n  books: () => [],\n};\n"
IMPORT_QUERIES = "import { queries } from './queries';\n"


@pytest.mark.parametrize("holder", [
    "const resolvers = { Query: queries };",
    "export const rootResolvers = { Query: queries };",
    "const map: Resolvers = { Query: queries };",
    "const map = { Query: queries } as Resolvers;",
    "const map = { Query: queries } satisfies Resolvers;",
    "const config = { typeDefs, resolvers: { Query: queries } };",
    "new ApolloServer({ typeDefs, resolvers: { Query: queries } });",
])
def test_named_import_root_in_a_resolver_map_is_read(analyze_files, holder) -> None:
    st = _run(analyze_files, {"src/server.ts": IMPORT_QUERIES + holder + "\n", "src/queries.ts": QUERIES_TS})
    assert {r.endpoint for r in _eps(st)} == {"books"}


@pytest.mark.parametrize("holder", [
    "export const bus = { Query: queries, Command: {} };",   # a CQRS bus
    "const handlers = { Query: queries };",
    "export default { Query: queries };",                    # no name to tell
    "const schemas = [{ Query: queries }];",
])
def test_named_import_root_that_is_not_in_a_resolver_map_is_none(analyze_files, holder) -> None:
    # Without a map named like resolvers, or a GraphQL schema in the repo, a `Query` key is not GraphQL.
    st = _run(analyze_files, {"src/server.ts": IMPORT_QUERIES + holder + "\n", "src/queries.ts": QUERIES_TS})
    assert _eps(st) == []


def test_named_import_root_is_read_where_the_repo_declares_a_schema(analyze_files) -> None:
    # `export default { Query: queries }` names nothing, but the repo has GraphQL SDL.
    files = {
        "src/server.ts": IMPORT_QUERIES + "export default { Query: queries };\n",
        "src/queries.ts": QUERIES_TS,
    }
    st = _run(analyze_files, {**files, "src/schema.graphql": "type Query { books: [Book] }\n"})
    assert {r.endpoint for r in _eps(st) if r.path == "src/server.ts"} == {"books"}
