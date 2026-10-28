# Ktor — entry-point roles

Parser: `src/breezeai_cog/parsers/kotlin_ktor/` · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_ktor.py`

| Construct | Role | Record | Why |
|---|---|---|---|
| `get("/x") { … }` / `post` / `put` / `delete` / `patch` / `head` / `options` inside `routing { }` | Binding | `route`, HTTP verb, `routeKind: route`, full path, `handlerLine` = the lambda (anonymous) | One handler, inbound API |
| Verb without a path inside `route("/p") { get { … } }` | Binding | `endpoint` = the enclosing prefix | Same, addressed by the prefix |
| A path from a constant (`get(USERS)`, `get(Paths.USERS)`, `"$API/x"`, `API + "/x"`) | — | resolved from the file's `val` / `const val`; a qualified one only through the `object` / class / `companion object` it names (`Admin.ITEMS` is never `Other.ITEMS`); unknown or two values → `endpoint` empty | Never a guessed address |
| `route("/p") { … }` | Wiring | statement stays plain; prefix on the nested bindings — a prefix that does not resolve (an unknown constant, `route(prefixFromEnv())`) leaves their `endpoint` empty | Points at no handler |
| `webSocket("/ws") { … }` / `webSocketRaw` | Binding | `route`, `method: WEBSOCKET`, `routeKind: ws`, path (with enclosing `route(...)` prefixes) | One handler, inbound WebSocket |
| `authenticate("name") { … }` | Guard | name in `guards` on the nested bindings; `authRequired: true` only when the block certainly rejects anonymous calls — not with `optional = true`, `strategy = AuthenticationStrategy.Optional`, or an option that can't be read | Runs before many handlers |
| `install(Plugin)` | None | — | Start-up configuration |
| `embeddedServer(...).start()` | None | — | Start-up |
| `HttpClient` calls (`client.get(url)`) | Outbound | `api_call` | Our code calling out |
