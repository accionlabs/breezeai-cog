"""GraphQL in TS / JS role table (docs/entry-point-roles/graphql-typescript.md): one test per row."""

from __future__ import annotations

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
    assert got == {("book", "ID", "Book"), ("addBook", "BookInput", "Book")}


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
    assert (r.requestDTO, r.responseDTO) == ("BookFilter", "Book")


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
        (r.id.split(":", 1)[1], r.method, r.routeKind, r.endpoint, r.handler, r.handlerLine)
        for r in _eps(st)
    }
    # One record per operation on the root key; handlerLine is empty because the function is
    # declared in another file (handlerPath isn't emitted yet).
    assert got == {
        ("5:2", "MUTATION", "mutation", "addBook", "addBook", None),
        ("6:2", "QUERY", "query", "dummyQuery", "dummyQuery", None),
        ("6:2#2", "QUERY", "query", "books", "books", None),
    }


def test_resolver_map_root_from_a_missing_module_is_none(analyze_files) -> None:
    # The imported module isn't in the repo: no operations are invented.
    st = _run(analyze_files, {"src/server.ts": SERVER_NS})
    assert _eps(st) == []
