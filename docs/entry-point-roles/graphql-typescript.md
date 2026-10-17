# GraphQL in TypeScript / JavaScript — entry-point roles

Parsers: `src/breezeai_cog/parsers/typescript_graphql/` (Apollo / graphql-tools resolver maps, embedded SDL, client operations) and `src/breezeai_cog/parsers/typescript_graphql_codefirst/` (in-house code-first decorators) · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_graphql_typescript.py`

NestJS resolvers are in [nestjs.md](nestjs.md); SDL root fields — in `.graphql` files and in `gql` `typeDefs` templates — are `route` records with their DTOs and no handler, the Target Spec's current form (the resolver-map entry is the binding)
follows [graphql-sdl.md](graphql-sdl.md); the Apollo transport mount is in [express.md](express.md).

| Construct | Role | Record | Why |
|---|---|---|---|
| Field of the `Query` / `Mutation` / `Subscription` object in a resolver map (`Query: { books: (…) => … }`) | Binding | `route`, `framework: graphql`, `QUERY` / `MUTATION` / `SUBSCRIPTION`, `routeKind` the same in lower case, field name as `endpoint` and `handler`, `handlerLine` = where the function starts; `requestDTO` / `responseDTO` from the field's SDL declaration (a `.graphql` file or an embedded `gql` schema, anywhere in the repo; empty if declared twice differently) | One operation; the property names the function |
| A root type given as a module (`import * as queries from './queries'` … `Query: queries`) | Binding | as above, one record per name the module exports (through `export { default as x } from './x'` and `export *` re-exports), on the `Query: queries` property; `handler` = the exported function's name, `handlerLine` empty (declared in another file). A module that isn't in the repo gives no record | One operation per exported function |
| Resolver-map object and `new ApolloServer({ typeDefs, resolvers })` | Wiring | — | Registers many operations |
| Field of a data-type object in a resolver map (`Book: { author: … }`) | None | — | No address of its own |
| `__resolveReference` of a type in a resolver map (federation) | Binding | `route`, `RESOLVE_REFERENCE`, `routeKind: query`, type name as `endpoint` | Called by the gateway's `_entities` field |
| `@Query(sdl)` / `@Mutation` / `@Subscription` on a method (code-first, decorators from a local `decorators` module) | Binding | `route`, `framework: graphql`, operation kind, operation name (leading identifier of the SDL fragment, else the method name) as `endpoint`, `handler` = method | One operation |
| `@FieldResolver` on a method (code-first) | None | — | Data-type field |
| `@RequireScopes` / `@RequireAPIScope` / `@Authorized` on a class or method (code-first) | Guard | names in `guards` | Run before the operations |
| `@Resolver()` class with `@Query` / `@Mutation` / `@Subscription` methods from `type-graphql` | Binding per method | as for code-first | One operation each |
| `@FieldResolver` from `type-graphql` | None | — | Data-type field |
| `gql` / `graphql` template or call holding a `query` / `mutation` / `subscription` document | Outbound | `api_call`, root field as `endpoint`, operation name in `handler` | Our code calling a GraphQL server |
