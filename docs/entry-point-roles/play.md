# Play — entry-point roles

Parser: `src/breezeai_cog/parsers/scala_play/` (`conf/routes`, `conf/*.routes` and Scala sources) · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_play.py`

| Construct | Role | Record | Why |
|---|---|---|---|
| `conf/routes` line `VERB /path controllers.C.action(...)` | Binding | `route`, HTTP verb, `routeKind: route`, path, `handler` = `action`, `handlerPath` = the controller's file, `handlerLine` = the action — the controller is a `<Class>.scala` / `.java` under the project's `app/` whose `package` is the route's; not found, in two files or overloaded (Play's own `controllers.Assets`) → `handler` as written, no location | One handler, inbound API — the record lives at the routes line |
| `conf/routes` line `-> /prefix some.Router` | Wiring | no record when it names a routes file of the repo (`admin.Routes` = `conf/admin.routes`, with at least one line) under a resolved prefix: the prefix is on that router's bindings, chained through nested includes. Any other router — a SIRD class, a generated gRPC router — keeps a `route`, `routeKind: mount`, `ANY` record, `handler` = the router (the Target Spec's current form); a SIRD class's bindings get the prefix too. Included twice or not at all → paths as written | Points at a router |
| `conf/routes` line `-> /webjars webjars.Routes` | Binding | `route`, `GET`, `endpoint` = the prefix, no handler | The WebJars module serves the assets |
| Controller method returning `Action { … }` / `Action.async { … }` | None | — (it is the handler of a routes line) | Has no address of its own |
| SIRD: `case GET(p"/x") => controller.action` in a `SimpleRouter` | Binding | `route`, HTTP verb, path, `handler` = `action` | One handler, inbound API |
| `EssentialFilter` / `Filter`, `ActionBuilder` / `ActionFilter` | Guard | name in `guards`; no record of its own | Run before many handlers |
| `WSClient` calls | Outbound | `api_call` | Our code calling out |
