# GraphQL SDL files — entry-point roles

Parser: `src/breezeai_cog/parsers/graphql/` (`.graphql` / `.gql` / `.graphqls`) · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_graphql_sdl.py`

The SDL declares the address of an operation, but the operation's handler is its resolver in
code (the resolver-map entry or resolver method). In the Target Spec's current form an SDL root
field is recorded as a `route` too, with its DTOs and no handler.

| Construct | Role | Record | Why |
|---|---|---|---|
| Field of `type Query` / `Mutation` / `Subscription` (or `extend type …`) | Wiring | `route`, `framework: graphql`, `QUERY` / `MUTATION` / `SUBSCRIPTION`, `endpoint` = the field, DTOs, no handler (the Target Spec's current form); the resolver for the field is also recorded and carries the same DTOs | Declares the operation's name; names no handler |
| `schema { query: RootQuery }` | Wiring | statement stays plain; makes `RootQuery`'s fields operations | Marks a root container |
| Field of a data type (`type Book { author: … }`) | None | — | No address of its own |
| `type Book @key(fields: …)` | None | `graphql_entity` (not an entry point); the entity resolver in code is the binding | Declares the entity, does not resolve it |
| `query` / `mutation` / `subscription` operation document | Outbound | `api_call`, operation field as `endpoint` | Our code calling a GraphQL server |
| `scalar` / `directive` / `enum` / `input` | None | — | Type system |
