# Akka HTTP — entry-point roles

Parser: `src/breezeai_cog/parsers/scala_akkahttp/` · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_akkahttp.py`

| Construct | Role | Record | Why |
|---|---|---|---|
| Verb directive (`get { … }`, `post`, `method(HttpMethods.X)`) under path directives, or `(get & path(...)) { … }` | Binding | `route`, HTTP verb, `routeKind: route`, path from the enclosing directives, `handlerLine` = the directive body (anonymous) | One handler, inbound API |
| Path directive (`path`, `pathPrefix`, `pathEnd`, …) with no verb inside | Binding | `route`, `method` empty, path | One handler for every verb |
| `path` / `pathPrefix` / `pathEnd` around verb directives | Wiring | statement stays plain; adds its segments to the nested bindings | Points at no handler |
| `authenticateBasic` / `authenticateOAuth2` / `authorize` directives | Guard | name in `guards` on the nested bindings | Run before many handlers |
| `Http().newServerAt(...).bind(route)` / `bindAndHandle` | None | — | Start-up |
| `Http().singleRequest(...)` | Outbound | `api_call` | Our code calling out |
