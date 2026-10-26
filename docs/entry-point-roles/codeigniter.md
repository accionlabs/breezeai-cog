# CodeIgniter 4 — entry-point roles

Parser: `src/breezeai_cog/parsers/php_codeigniter/` · Rules: [entry-point contract](../architecture/entry-points.md) ·
Tests: `tests/unit/test_roles_codeigniter.py`

A handler is written as the method name with `handlerPath` / `handlerLine` when its declaration
is found (`php/handlers.py`), else as registered (`Users@show`).

| Construct | Role | Record | Why |
|---|---|---|---|
| `$routes->get/post/put/patch/delete/options/head(path, 'Users::show/$1')` | Binding | `route`, the verb, `routeKind: route`, full path (leading `/`), handler `show` (the `/$1` argument mapping is dropped) | One handler, inbound API |
| `$routes->add/any(path, handler)` | Binding | `route`, `ANY` | One handler for every verb |
| `$routes->match(['get', 'post'], path, handler)` | Binding | one `route` per verb | One handler per verb |
| Placeholders `(:num)` `(:segment)` `(:any)` `(:alpha)` `(:alphanum)` `(:hash)` | — | `isRegex: true` on the route | The path is a pattern |
| A path that is not a literal | Binding | `route`, empty `endpoint` | The address can't be resolved (contract §8) |
| Inline closure handler | Binding | empty `handler`, `handlerLine` = where the closure starts | Anonymous handler |
| `$routes->view(path, view)` / `$routes->addRedirect(path, target)` | Binding | `route`, `GET`, empty `handler` | CodeIgniter serves it; a view / target is not a handler |
| `$routes->cli(…)` | None | — | Command line, not an inbound request |
| `$routes->resource(name)` / `$routes->presenter(name)` | Wiring that makes one binding per action | one `route` per generated route (`only` / `except` / `placeholder` / `controller` applied) | Expands to many handlers |
| `$routes->map(['path' => handler, …])` | Wiring that makes one binding per entry | one `route`, `ANY`, per literal entry | Each entry names one handler |
| `$routes->group(prefix, ['filter' => f], fn)` | Wiring | no record; a literal prefix is joined into the nested paths, a dynamic one leaves them empty; the filter goes in `guards` | Applies to many handlers |
| Route options `['filter' => f, 'as' => name]` | Guard / name | `guards`, `name` | Runs before the handler |
| `$routes->environment('development', fn)` | Wiring | no record; the routes inside are recorded | Scopes routes to an environment |
| `Events::on(name, handler)` | Binding | `eventbus_consumer`, `CONSUMER`, endpoint = the name | Inbound event consumer |
| `Events::trigger(name)` | Outbound | `eventbus_publish`, no `method` | Our code emits |
