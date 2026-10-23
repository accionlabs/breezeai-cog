# Laravel — entry-point roles

| Construct | Role | Record | Why |
|---|---|---|---|
| `Route::get/post/...` with a resolvable path | Binding | `route`, HTTP method, `routeKind: route`, path endpoint | Inbound HTTP binding |
| `Route::view(path, view)` | Binding | `route`, `GET`, `routeKind: route`, path endpoint, empty handler | The view name is not a handler address |
| `Route::redirect(path, target)` | Binding | `route`, `GET`, `routeKind: route`, path endpoint, empty handler | The target is not a handler address |
| `Route::resource/apiResource/singleton/apiSingleton` | Binding per REST binding | one `route` record per HTTP binding | Each generated binding is independently addressable |
| `Route::prefix(...)->group(...)` | Wiring | no record; prefix copied only when literal | A dynamic prefix cannot produce a truthful endpoint |
| `Event::dispatch` / `Bus::dispatch` | Outbound | `eventbus_publish` / `eventbus_send`, `PUBLISH` / `MESSAGE` | Application code emits an event/message |
| `Event::listen` | Binding | `eventbus_consumer`, `CONSUMER` | Inbound event consumer |
