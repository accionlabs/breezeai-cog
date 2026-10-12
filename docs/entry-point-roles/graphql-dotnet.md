# graphql-dotnet — entry-point roles

Parser: `src/breezeai_cog/parsers/csharp_graphql/` · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_graphql_dotnet.py`

| Construct | Role | Record | Why |
|---|---|---|---|
| Root operation type (`class BookQuery : ObjectGraphType`, assigned to `Schema.Query` / `Mutation` / `Subscription`) | Wiring | — | A container: each field is a binding |
| `Schema` constructor `Query = …` / `Mutation = …` | Wiring | statement stays plain | Names the root types; no handler |
| `Field<T>("x")` / `FieldAsync` / `AddField` on a root type | Binding | `route`, `QUERY` / `MUTATION` / `SUBSCRIPTION`, matching `routeKind`, `x` as `endpoint` | One operation |
| `.Resolve(ctx => …)` on that field | — | `handler` empty, `handlerLine` = the lambda | Inline resolver is anonymous |
| `.Resolve(ResolveX)` on that field | — | `handler: ResolveX`, `handlerLine` = its declaration | Named resolver |
| Field of a data type (`ObjectGraphType<Book>`) | None | — | No address of its own |
