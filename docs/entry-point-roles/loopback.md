# LoopBack 4 — entry-point roles

Parser: `src/breezeai_cog/parsers/typescript_loopback/` · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_loopback.py`

| Construct | Role | Record | Why |
|---|---|---|---|
| `@get(path)` / `@post` / `@put` / `@patch` / `@del` / `@head` / `@options` on a controller method | Binding | `route`, HTTP verb, `routeKind: route`, full path as `endpoint`, `handler` = method, `handlerLine` = its declaration | One handler, inbound API |
| `@operation(verb, path)` on a method | Binding | as above | Generic form of the same |
| `@api({ basePath })` on a controller class | Wiring | — (prefix for the class's bindings; a `basePath` that is not a literal is a value read at runtime: `${basePath}/…`, never its source text, [contract §6](../architecture/entry-points.md#a-part-read-at-runtime-name)) | Container, no handler |
| `rest: { basePath }` in the application config / `app.basePath(path)` | Wiring | — (prefix for every binding; one read at runtime, e.g. from env, is marked: `${BASE_PATH}/…`, [contract §6](../architecture/entry-points.md#a-part-read-at-runtime-name)) | Applies to all handlers |
| Path with an unresolved part (`appConfig.apiPath + '/x'`) | Binding | `endpoint` empty | Never a partial address or a code name |
| `@authenticate(strategy)` on a class or method | Guard | strategy names in `guards` | Runs before the handlers |
| `@authenticate.skip()` | None | — | Removes a guard, adds nothing |
| `app.start()` / `main()` | None | — | Start-up |
