# Play — entry-point roles

Parser: `src/breezeai_cog/parsers/scala_play/` (`conf/routes` and Scala sources) · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_play.py`

| Construct | Role | Record | Why |
|---|---|---|---|
| `conf/routes` line `VERB /path controllers.C.action(...)` | Binding | `route`, HTTP verb, `routeKind: route`, path, `handler` = `action`, `handlerPath` = the controller's file | One handler, inbound API — the record lives at the routes line |
| `conf/routes` line `-> /prefix some.Router` | Wiring | statement stays plain; prefix on that router's bindings | Points at a router |
| Controller method returning `Action { … }` / `Action.async { … }` | None | — (it is the handler of a routes line) | Has no address of its own |
| SIRD: `case GET(p"/x") => controller.action` in a `SimpleRouter` | Binding | `route`, HTTP verb, path, `handler` = `action` | One handler, inbound API |
| `EssentialFilter` / `Filter`, `ActionBuilder` / `ActionFilter` | Guard | name in `guards`; no record of its own | Run before many handlers |
| `WSClient` calls | Outbound | `api_call` | Our code calling out |
