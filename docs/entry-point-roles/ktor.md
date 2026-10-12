# Ktor — entry-point roles

Parser: `src/breezeai_cog/parsers/kotlin_ktor/` · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_ktor.py`

| Construct | Role | Record | Why |
|---|---|---|---|
| `get("/x") { … }` / `post` / `put` / `delete` / `patch` / `head` / `options` inside `routing { }` | Binding | `route`, HTTP verb, `routeKind: route`, full path, `handlerLine` = the lambda (anonymous) | One handler, inbound API |
| Verb without a path inside `route("/p") { get { … } }` | Binding | `endpoint` = the enclosing prefix | Same, addressed by the prefix |
| `route("/p") { … }` | Wiring | statement stays plain; prefix on the nested bindings | Points at no handler |
| `webSocket("/ws") { … }` | Binding | `route`, `routeKind: ws`, path | One handler, inbound WebSocket |
| `authenticate("name") { … }` | Guard | name in `guards` on the nested bindings | Runs before many handlers |
| `install(Plugin)` | None | — | Start-up configuration |
| `embeddedServer(...).start()` | None | — | Start-up |
| `HttpClient` calls (`client.get(url)`) | Outbound | `api_call` | Our code calling out |
