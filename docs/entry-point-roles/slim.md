# Slim — entry-point roles

Parser: `src/breezeai_cog/parsers/php_slim/` · Rules: [entry-point contract](../architecture/entry-points.md) ·
Tests: `tests/unit/test_roles_slim.py`

A handler is written as the method name with `handlerPath` / `handlerLine` when its declaration
is found (`php/handlers.py`), else as registered (`HomeAction@show`).

| Construct | Role | Record | Why |
|---|---|---|---|
| `$app->get/post/put/patch/delete/options/any(path, handler)` (also `$group->…` inside a group) | Binding | `route`, the verb (`any` → `ANY`), `routeKind: route`, full path; `'Cls:method'` → `Cls@method`, `Cls::class` → its `__invoke` | One handler, inbound API |
| `$app->map(['GET', 'POST'], path, handler)` | Binding | one `route` per verb | One handler per verb |
| Inline closure handler | Binding | empty `handler`, `handlerLine` = where the closure starts | Anonymous handler |
| `$app->group(prefix, function ($group) { … })` | Wiring | no record; a literal prefix is joined into the nested paths, a dynamic one leaves them empty | Applies to many handlers |
| `->add(middleware)` on a route or group | Guard | names in `guards` | Runs before the handler(s) |
| `->setName(name)` | — | `name` on the route | Route name |
| `$app->redirect(from, to)` | Binding | `route`, `GET`, `routeKind: route`, empty `handler` | Slim serves the redirect; the target is a URL, not a handler |
| `$app->run()` / `AppFactory::create()` | None | — | Start-up |
