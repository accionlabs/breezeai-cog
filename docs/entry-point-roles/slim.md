# Slim — entry-point roles

| Construct | Role | Record | Why |
|---|---|---|---|
| `$app/$group->get/post/...` with a literal path | Binding | `route`, HTTP method, `routeKind: route` | Inbound HTTP binding |
| Inline closure handler | Binding | same route; empty handler and closure start in `handlerLine` | Closure has no addressable handler name |
| `group(prefix, ...)` | Wiring | literal prefix copied; unresolved prefix leaves endpoint empty | Prefix is not itself an entry point |
| `redirect(path, target)` | Binding | `route`, `GET`, `routeKind: route`, path endpoint, empty handler | Slim serves the redirect itself; the target is a URL, not a handler |
