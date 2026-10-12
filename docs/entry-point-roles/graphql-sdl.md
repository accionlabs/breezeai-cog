# GraphQL SDL files — entry-point roles

Parser: `src/breezeai_cog/parsers/graphql/` (`.graphql` / `.gql` / `.graphqls`) · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_graphql_sdl.py`

The SDL declares the address of an operation; its resolver lives in code, so `handler` is
filled only when the resolver can be located.

| Construct | Role | Record | Why |
|---|---|---|---|
| Field of `type Query` / `Mutation` / `Subscription` (or `extend type …`) | Binding | `route`, `QUERY` / `MUTATION` / `SUBSCRIPTION`, `routeKind` = the same in lower case, field name as `endpoint` | One operation, addressed by name |
| `schema { query: RootQuery }` | Wiring | statement stays plain; makes `RootQuery`'s fields operations | Marks a root container |
| Field of a data type (`type Book { author: … }`) | None | — | No address of its own |
| `type Book @key(fields: …)` | None | `graphql_entity` (not an entry point); the entity resolver in code is the binding | Declares the entity, does not resolve it |
| `query` / `mutation` / `subscription` operation document | Outbound | `api_call`, operation field as `endpoint` | Our code calling a GraphQL server |
| `scalar` / `directive` / `enum` / `input` | None | — | Type system |
