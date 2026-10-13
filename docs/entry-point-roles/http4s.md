# http4s — entry-point roles

Parser: `src/breezeai_cog/parsers/scala_http4s/` · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_http4s.py`

| Construct | Role | Record | Why |
|---|---|---|---|
| `case VERB -> Root / "x" / id => …` in `HttpRoutes.of` | Binding | `route`, HTTP verb, `routeKind: route`, path (extractors as `{name}`), `handlerLine` = the case (anonymous) | One handler, inbound API |
| `case VERB -> Root / … as user => …` in `AuthedRoutes.of` | Binding | as above | Same, with an authenticated user |
| `Router("/api" -> routes)` | Wiring | statement stays plain; prefix on the mounted routes | Points at a route set |
| `AuthMiddleware(...)` applied to routes | Guard | name in `guards` on those bindings | Runs before many handlers |
| `EmberServerBuilder` / `BlazeServerBuilder` … `.serve` | None | — | Start-up |
| `Client` calls (`client.expect`, `client.run`) | Outbound | `api_call` | Our code calling out |
