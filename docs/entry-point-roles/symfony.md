# Symfony — entry-point roles

| Construct | Role | Record | Why |
|---|---|---|---|
| `#[Route(...)]` on a method | Binding | `route`, declared method or `ANY`, `routeKind: route` | Inbound HTTP binding |
| Routing builder `add(...)->controller(...)` | Binding | one `route` per declared method; empty handler when controller is absent | Builder call is the binding site |
| `RouteCollection` / routing configurator wiring | Wiring | no separate record | Collection construction supplies route metadata |
| `$dispatcher->addListener(name, callback)` | Binding | `eventbus_consumer`, `CONSUMER`, handler from the callback | Inbound event consumer; detected additively (`php_symfony/events.py`) in any PHP file |
| `$dispatcher->dispatch($event, name)` | Outbound | `eventbus_publish`, endpoint = the name argument | Application emits an event |
| `$dispatcher->addSubscriber($s)` | Wiring | no record | A subscriber registers many handlers |
| Messenger dispatch | Outbound | `eventbus_send`, no `method` | Application emits a message |
