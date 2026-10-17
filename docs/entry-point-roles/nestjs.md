# NestJS — entry-point roles

Parser: `src/breezeai_cog/parsers/typescript_nestjs/` (also routing-controllers and look-alike decorator modules) · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_nestjs.py`

| Construct | Role | Record | Why |
|---|---|---|---|
| `@Get(path)` / `@Post` / `@Put` / `@Patch` / `@Delete` / `@All` … on a controller method | Binding | `route`, HTTP verb, `routeKind: route`, full path as `endpoint`, `handler` = method, `handlerLine` = its declaration | One handler, inbound API |
| `@Get([':id', ':slug'])` — an array of paths (method or `@Controller`) | Binding, one per path | as above, one record per controller path × method path, same handler; an element that is not a string literal → that record's `endpoint` empty | Nest serves the handler at each path |
| `@Controller(path)` / `@JsonController(path)` on a class | Wiring | — (prefix for the class's bindings) | Container, no handler |
| `app.setGlobalPrefix(prefix)` in the bootstrap | Wiring | — (prefix for every HTTP binding) | Applies to all handlers |
| `RouterModule.register([{ path, module }])` | Wiring | — (prefix for the module's controllers) | Points at a module |
| `@Version(v)` on a method or controller | Wiring | — (`version` on the bindings; part of the path under URI versioning) | Adds to other bindings |
| `@UseGuards(...)` / `@Authorized()` on a class or method | Guard | names in `guards` | Run before the handlers |
| `@UseInterceptors(...)` on a class or method | Guard | names in `guards` | Run before the handlers |
| `@EventPattern(pattern)` | Binding | `eventbus_consumer`, `EVENT`, `routeKind: message`, pattern as `endpoint` (object → canonical JSON, as above) | Event consumer on a transport |
| `@MessagePattern(pattern)` | Binding | `eventbus_consumer`, `MESSAGE`, `routeKind: message`, pattern as `endpoint` — a string as written, an object as canonical JSON with sorted keys (`{"cmd":"sum"}`) | Request-response message consumer |
| `@Cron(expr)` / `@Interval(ms)` / `@Timeout(ms)` (`@nestjs/schedule`) | Binding | `timer`, `PERIODIC` (`TIMER` for `@Timeout`), schedule as `endpoint` (a `CronExpression` member → its expression, fixed by the library; a numeric constant → its value), `handler` = method | Scheduled job |
| `@Resolver()` on a class | Wiring | — | Container of GraphQL operations |
| `@Query` / `@Mutation` / `@Subscription` on a resolver method | Binding | `route`, `framework: graphql`, `QUERY` / `MUTATION` / `SUBSCRIPTION`, `routeKind` the same in lower case, operation name as `endpoint` | One GraphQL operation |
| `@ResolveField` on a data-type resolver (`@Resolver(() => Book)`) | None | `route`, `RESOLVE_FIELD`, `routeKind: field_resolver` (not an entry point) | No address of its own |
| `@ResolveField` on a stub parent (a type returned only by a root `@Mutation` / `@Query` as an empty object) | Binding | `route`, the parent's operation type, `endpoint` = `<StubType>.<field>` | Stub-parent convention: its fields are the operations |
| `@ResolveReference()` (federation) | Binding | `route`, `RESOLVE_REFERENCE`, `routeKind: query`, type name as `endpoint` | Called by the gateway's `_entities` field |
| `NestFactory.create(...)` / `app.listen(port)` | None | — | Start-up |
| `HttpService` / `fetch` calls | Outbound | `api_call` | Our code calling out |
| `ClientProxy.send(...)` / `.emit(...)` | Outbound | `eventbus_send` / `eventbus_publish`, pattern as `endpoint` written exactly like the consumer's | Our code sending a message |
