# Symfony — entry-point roles

| Construct | Role | Record | Why |
|---|---|---|---|
| `#[Route(...)]` on a method | Binding | `route`, declared method or `ANY`, `routeKind: route` | Inbound HTTP binding |
| Routing builder `add(...)->controller(...)` | Binding | one `route` per declared method; empty handler when controller is absent | Builder call is the binding site |
| `RouteCollection` / routing configurator wiring | Wiring | no separate record | Collection construction supplies route metadata |
| Event dispatcher listener registration | Binding | `eventbus_consumer`, `CONSUMER` | Inbound event consumer |
| Messenger dispatch | Outbound | `eventbus_send`, no `method` | Application emits a message |
