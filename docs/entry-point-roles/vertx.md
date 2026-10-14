# Vert.x (Java and Groovy) — entry-point roles

Parser: `src/breezeai_cog/parsers/vertx_common.py` (shared rules), `java_vertx/`, `groovy_vertx/` · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_vertx.py`

| Construct | Role | Record | Why |
|---|---|---|---|
| `router.get("/x").handler(h)` / `router.post(...)` / … on a `Router` | Binding | `route`, HTTP verb, `routeKind: route`, path, `handler` = `h` (anonymous lambda: `handlerLine` only) | One handler, inbound API |
| `router.route("/x").handler(h)` (no verb) | Binding | `route`, `method` empty, path | One handler for every verb |
| Vert.x 2.x `RouteMatcher`: `rm.get("/x", h)`, bare `get(...)` inside `rm.with { }` | Binding | as above | Same construct, older API |
| `router.route().handler(h)` (no path) / `BodyHandler`, `AuthenticationHandler` | Guard | name in `guards` on the router's bindings; no record of its own | Runs before many handlers |
| `router.route("/api/*").subRouter(sub)` / `mountSubRouter("/api", sub)` | Wiring | statement stays plain; prefix on `sub`'s bindings | Points at a router |
| `eventBus.consumer(addr, h)` / `localConsumer` / 2.x `registerHandler` / `registerLocalHandler` | Binding | `eventbus_consumer`, address as `endpoint`, `handler` = `h` | One handler, inbound message |
| Private helper that forwards its first `String` parameter to `consumer` / `registerHandler` | Wiring | each call of the helper is the binding, with its own address | The helper's address is a parameter |
| `vertx.setPeriodic(ms, h)` / `vertx.setTimer(ms, h)` | Binding | `timer`, `method: PERIODIC` / `TIMER`, interval as `endpoint`, `handler` = `h` | One handler, triggered by a schedule |
| `new ServiceBinder(vertx).setAddress(addr).register(Iface, impl)` | Binding, one per service method | `eventbus_consumer`, `endpoint` = address, `handler` = the method (from the interface, any file; `default` / `static` / `@ProxyIgnore` methods are not proxied); interface unknown → one record, no handler | A service on a message address |
| `new ServiceProxyBuilder(vertx).setAddress(addr).build(Iface)` | Outbound | `eventbus_send`, `endpoint` = address | A client of that service |
| `@ProxyGen` interface | None | — | Service contract only |
| `eventBus.send` / `request` / 2.x `sendWithTimeout` | Outbound | `eventbus_send` | Our code sending |
| `eventBus.publish` | Outbound | `eventbus_publish` | Our code publishing |
| `vertx.deployVerticle(...)` | None | (structural `verticle_deploy` statement) | Start-up |
| `vertx.createHttpServer()…listen(port)` | None | — | Start-up |
