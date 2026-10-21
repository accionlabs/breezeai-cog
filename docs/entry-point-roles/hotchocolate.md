# HotChocolate — entry-point roles

Parser: `src/breezeai_cog/parsers/csharp_hotchocolate/` · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_hotchocolate.py`

| Construct | Role | Record | Why |
|---|---|---|---|
| `[QueryType]` / `[MutationType]` / `[SubscriptionType]` on a class | Wiring | — | Marks a root container: each public method is a binding |
| `[ExtendObjectType(OperationTypeNames.Query)]` (or `"Query"`, a root type) | Wiring | — | Same, for an extension of a root |
| `AddQueryType<T>()` / `AddMutationType<T>()` / `AddSubscriptionType<T>()` | Wiring | statement stays plain; `T` is bound as the compiler binds it (the registering file's `using`s, global usings) — a type not in scope there is not bound | Registers `T` as a root container, usually from another file |
| Public method of a root container | Binding | `route`, `QUERY` / `MUTATION` / `SUBSCRIPTION`, matching `routeKind`, field name as `endpoint`, `handler` = the method | One operation |
| Non-public member, or `[GraphQLIgnore]` | None | — | Not in the schema |
| `descriptor.Field("x")` in `Configure` of `ObjectType<Query>` / an extension of a root | Binding | `route`, operation kind, `x`; `handler`: `.ResolveWith<R>(r => r.M())` → `M`, its line, and `handlerPath` when `R` is in another file; resolver lambda → empty, `handlerLine` = the lambda; `Field(q => q.M())` → `M`; otherwise empty — never the field name | One operation, declared fluently |
| `[Subscribe]` + `[Topic("t")]` on a subscription | Binding | `eventbus_consumer`, `CONSUMER`, `t` as `endpoint` (in addition to the subscription's `route`) | The method consumes the topic |
| Field of a data type (`[ExtendObjectType(typeof(Book))]` with `[Parent]`, `ObjectType<Book>` fields) | None | `route`, `routeKind: field_resolver`, `RESOLVE_FIELD` (not an entry point) | No address of its own |
| `[ReferenceResolver]` (Apollo Federation) | Binding | `route`, `RESOLVE_REFERENCE`, `routeKind: query`, type name as `endpoint` | Called by the generated `_entities` field |
| `[NodeResolver]` on a `[Node]` type (Relay); on a query root it is also a query field (two records) | Binding | `route`, `RESOLVE_REFERENCE`, `routeKind: query`, type name as `endpoint` | Called by the generated `node` field |
| `descriptor.ImplementsNode().ResolveNode(…)` / `ResolveNodeWith<T>(…)` / `ResolveReferenceWith(…)` | Binding | `route`, `RESOLVE_REFERENCE`, `routeKind: query`, the descriptor's type as `endpoint`; inline lambda → `handlerLine` only | Fluent form of the two rows above |
| `[Authorize]` / `.Authorize(...)` | Guard | name in `guards` | |
| `MapGraphQL("/graphql")` | Wiring | `route`, `POST /graphql`, `framework: graphql`, no handler — the Target Spec's current form | Transport path; operations are addressed by name |
| `AddGraphQLServer()` | None | — | Start-up |
