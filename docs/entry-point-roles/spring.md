# Spring — entry-point roles

Parser: `src/breezeai_cog/parsers/java_springboot/` · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_spring.py`

| Construct | Role | Record | Why |
|---|---|---|---|
| `@GetMapping` / `@PostMapping` / `@PutMapping` / `@DeleteMapping` / `@PatchMapping` on a method of a `@RestController` / `@Controller` | Binding | `route`, HTTP verb, `routeKind: route`, full path, `handler` = method | One handler, inbound API |
| A mapping with several paths (`@GetMapping({"/a", "/b"})`) | Binding, one per path | one `route` record per path | One binding, one record |
| `@RequestMapping(value = …, method = …)` on a method | Binding | `route`, the verb from `method` | Same as a verb mapping |
| `@RequestMapping` on a controller class | Wiring | statement stays plain; its path prefixes the class's bindings | Points at no handler |
| `server.servlet.context-path` in `application.properties` / `.yml` | Wiring | prefix on every HTTP binding's `endpoint` | Applies to all handlers |
| Functional route: `route(GET("/x"), h::list)` / builder `.GET("/x", h::list)` | Binding | `route`, HTTP verb, path, `handler` = the referenced method; `handlerPath` / `handlerLine` = its declaration in the receiver's class (`h`'s parameter or field type, or `Type::m`) | One handler, inbound API |
| Functional `nest(path("/p"), …)` / `.path("/p", …)` | Wiring | prefix on the nested bindings | Points at no handler |
| `@KafkaListener` / `@RabbitListener` / `@JmsListener` / `@SqsListener` on a method | Binding | `eventbus_consumer`, topic or queue as `endpoint`, `handler` = method | One handler, inbound message |
| `@Scheduled(cron = …)` / `@Scheduled(fixedRate = …)` / `fixedDelay` on a method | Binding | `timer`, cron or interval as `endpoint`, `handler` = method | One handler, triggered by a schedule |
| `@PreAuthorize` / `@Secured` / `@RolesAllowed` on a handler | Guard | name in `guards` of that binding | Runs before the handler |
| `HandlerInterceptor`, servlet `Filter`, `OncePerRequestFilter` | Guard | name in `guards`; no record of its own | Runs before many handlers |
| `RestTemplate` / `WebClient` / Feign client calls | Outbound | `api_call` | Our code calling out |
| `SpringApplication.run(...)` | None | — | Start-up |
| Other methods of a controller (no mapping) | None | — | Not inbound |
