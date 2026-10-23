# JAX-RS — entry-point roles

Parser: `src/breezeai_cog/parsers/java_jaxrs/` · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_jaxrs.py`

| Construct | Role | Record | Why |
|---|---|---|---|
| `@GET` / `@POST` / `@PUT` / `@DELETE` / `@PATCH` / `@HEAD` / `@OPTIONS` on a resource method | Binding | `route`, HTTP verb, `routeKind: route`, full path, `handler` = method | One handler, inbound API |
| `@Path` on that method | Part of the binding | joined after the class path in `endpoint` | The method's own address part |
| `@Path` on a root resource class | Wiring | statement stays plain; prefixes the class's bindings | Points at no handler |
| `@ApplicationPath` on the `Application` subclass | Wiring | prefix on every binding's `endpoint` | Applies to all handlers |
| Sub-resource locator: `@Path` on a method with no verb, returning a resource | Wiring | prefix on the returned class's bindings | Points at a resource, not one handler |
| `@RolesAllowed` / `@DenyAll` on a handler or its class | Guard | name in `guards` of that binding, `authRequired: true`. A method annotation replaces the class one: `@PermitAll` on the method leaves it with no guard | Runs before the handler |
| `@PermitAll` | None | — (not a guard; on a method it removes the class's `@RolesAllowed` / `@DenyAll`) | Opens access |
| `ContainerRequestFilter` / `@NameBinding` filters | Guard | name in `guards`; no record of its own | Runs before many handlers |
| JAX-RS client (`ClientBuilder…target(…).request().get()`) | Outbound | `api_call` | Our code calling out |
| `Application` subclass without `@ApplicationPath` | None | — | Start-up |
