# CodeIgniter — entry-point roles

| Construct | Role | Record | Why |
|---|---|---|---|
| `$routes->get/post/...` | Binding | `route`, HTTP method, `routeKind: route` | Inbound HTTP binding |
| `$routes->view(path, view)` | Binding | `route`, `GET`, `routeKind: route`, path endpoint, empty handler | View name is not a handler |
| `$routes->addRedirect(path, target)` | Binding | `route`, `GET`, `routeKind: route`, path endpoint, empty handler | Redirect target is not a handler |
| `$routes->cli(...)` | None | no entry-point record | CLI wiring is outside the HTTP route contract |
| `$routes->group(prefix, ...)` | Wiring | no record; literal prefix copied | Dynamic prefix has no truthful endpoint |
| `Events::on/trigger` | Binding / Outbound | consumer `CONSUMER`; publish `eventbus_publish` with no `method` | Event registration or emission |
