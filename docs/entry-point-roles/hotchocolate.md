# HotChocolate — entry-point roles

Parser: `src/breezeai_cog/parsers/csharp_hotchocolate/` · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_hotchocolate.py`

| Construct | Role | Record | Why |
|---|---|---|---|
| `[QueryType]` / `[MutationType]` / `[SubscriptionType]` on a class | Wiring | — | Marks a root container: each public method is a binding |
| `[ExtendObjectType(OperationTypeNames.Query)]` (or `"Query"`, a root type) | Wiring | — | Same, for an extension of a root |
| `AddQueryType<T>()` / `AddMutationType<T>()` / `AddSubscriptionType<T>()` | Wiring | statement stays plain | Registers `T` as a root container, usually from another file |
| Public method of a root container | Binding | `route`, `QUERY` / `MUTATION` / `SUBSCRIPTION`, matching `routeKind`, field name as `endpoint`, `handler` = the method | One operation |
| Non-public member, or `[GraphQLIgnore]` | None | — | Not in the schema |
| `descriptor.Field("x")` in `Configure` of `ObjectType<Query>` / an extension of a root | Binding | `route`, operation kind, `x`; resolver lambda → `handler` empty, `handlerLine` = the lambda | One operation, declared fluently |
| `[Subscribe]` + `[Topic("t")]` on a subscription | Binding | `eventbus_consumer`, `CONSUMER`, `t` as `endpoint` (in addition to the subscription's `route`) | The method consumes the topic |
| Field of a data type (`[ExtendObjectType(typeof(Book))]` with `[Parent]`, `ObjectType<Book>` fields) | None | `route`, `routeKind: field_resolver`, `RESOLVE_FIELD` (not an entry point) | No address of its own |
| `[ReferenceResolver]` (Apollo Federation) | Binding | `route`, `RESOLVE_REFERENCE`, `routeKind: query`, type name as `endpoint` | Called by the generated `_entities` field |
| `[NodeResolver]` on a `[Node]` type (Relay) | Binding | `route`, `RESOLVE_REFERENCE`, `routeKind: query`, type name as `endpoint` | Called by the generated `node` field |
| `[Authorize]` / `.Authorize(...)` | Guard | name in `guards` | |
| `MapGraphQL("/graphql")` | Wiring | statement stays plain | Transport path; operations are addressed by name |
| `AddGraphQLServer()` | None | — | Start-up |
