# Spring — entry-point roles

Parser: `src/breezeai_cog/parsers/java_springboot/` · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_spring.py`

| Construct | Role | Record | Why |
|---|---|---|---|
| `@GetMapping` / `@PostMapping` / `@PutMapping` / `@DeleteMapping` / `@PatchMapping` on a method of a `@RestController` / `@Controller` | Binding | `route`, HTTP verb, `routeKind: route`, full path, `handler` = method | One handler, inbound API |
| A mapping with several paths (`@GetMapping({"/a", "/b"})`) | Binding, one per path | one `route` record per path | One binding, one record |
| `@RequestMapping(value = …, method = …)` on a method | Binding | `route`, the verb from `method` | Same as a verb mapping |
| `@RequestMapping` on a controller class | Wiring | statement stays plain; its path prefixes the class's bindings | Points at no handler |
| `server.servlet.context-path` in `application.properties` / `.yml` | Wiring | prefix on every HTTP binding's `endpoint` | Applies to all handlers served by the servlet: annotated controllers and WebMvc.fn routes. A module without one, in a repo where another module sets one, may be served under it: its routes' `endpoint` is empty. Not to WebFlux.fn routes (`org.springframework.web.reactive.function`), which the servlet does not serve — nor do servlet filters and MVC interceptors guard them |
| `spring.webflux.base-path` in `application.properties` / `.yml` | Wiring | prefix on a WebFlux.fn route's `endpoint` | A `${…}` placeholder resolves from the module's base config; unresolved → empty `endpoint` |
| Functional route: `route(GET("/x"), h::list)` / builder `.GET("/x", h::list)` | Binding | `route`, HTTP verb, path, `handler` = the referenced method; `handlerPath` / `handlerLine` = its declaration in the receiver's class (`h`'s parameter or field type, or `Type::m`) | One handler, inbound API |
| Functional `nest(path("/p"), …)` / `.path("/p", …)` | Wiring | prefix on the nested bindings | Points at no handler |
| `@KafkaListener` / `@RabbitListener` / `@JmsListener` / `@SqsListener` on a method | Binding | `eventbus_consumer`, topic or queue as `endpoint`, `handler` = method | One handler, inbound message |
| `@Scheduled(cron = …)` / `@Scheduled(fixedRate = …)` / `fixedDelay` on a method | Binding | `timer`, cron or interval as `endpoint` (an interval in milliseconds: `timeUnit = TimeUnit.SECONDS` is converted; a non-literal unit → empty; an ISO-8601 duration in a `…String` form, `"PT5S"`, is kept as written), `handler` = method | One handler, triggered by a schedule |
| `@PreAuthorize` / `@Secured` / `@RolesAllowed` on a handler or its class | Guard | name in `guards` of that binding; a method annotation replaces the class one of the same kind. `authRequired: true` only for a check that needs a signed-in caller — a role, or `hasRole` / `hasAnyRole` / `hasAuthority` / `hasAnyAuthority` / `isAuthenticated` / `isFullyAuthenticated` not combined with `or` or a negation. `@PreAuthorize("permitAll()")` is no guard; any other expression is listed without a claim | Runs before the handler |
| `HandlerInterceptor`, servlet `Filter`, `OncePerRequestFilter` | Guard | name in `guards`; no record of its own | Runs before many handlers. A filter that overrides `shouldNotFilter` is in no route's `guards`: which requests it skips is decided at run time |
| `RestTemplate` / `WebClient` / Feign client calls | Outbound | `api_call` | Our code calling out |
| `SpringApplication.run(...)` | None | — | Start-up |
| Other methods of a controller (no mapping) | None | — | Not inbound |
