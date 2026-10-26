# Backbone — entry-point roles

Parser: `src/breezeai_cog/parsers/typescript_backbone/` (additive detector, every TS / JS file) ·
Rules: [entry-point contract](../architecture/entry-points.md) ·
Tests: `tests/unit/test_roles_backbone.py`

| Construct | Role | Record | Why |
|---|---|---|---|
| `Backbone.Router.extend({ routes: { path: handler } })` / `extends Backbone.Router` with a literal `routes` table | Binding | `route`, `routeKind: page`, `framework: backbone`, `endpoint` = route path joined to a literal `Backbone.history.start({ root })`, `handler` = route handler | A client-side page reached by URL |
| `router.route(path, name, callback)` / `router.route(path, name)` | Binding | `route`, `routeKind: page`; literal path and resolvable handler are captured | Runtime route registration |
| A non-literal route-map path or a `RegExp` passed to `router.route(...)` | None | — | No concrete route path is available |
| A non-literal history root | Binding | Routes keep their literal paths without a base prefix | Never guess a base path |
| Model `fetch()` / `save()` / `destroy()` | Outbound | `api_call`; `GET` / `PUT` or `POST` / `DELETE`; literal `url` or `urlRoot` used as endpoint where resolvable | A model request to the server |
| Collection `fetch()` / `create()` | Outbound | `api_call`; `GET` / `POST`; literal `url` used as endpoint where resolvable | A collection request to the server |
| `Backbone.sync(method, modelOrCollection)` | Outbound | `api_call`; method mapped from Backbone's `read`, `create`, `update`, `patch`, or `delete` operation | Explicit sync request |
| A model or collection endpoint that cannot be resolved to a literal | Outbound | `api_call` with a null endpoint | Do not stringify a variable or expression |
| `Backbone.View.extend(...)` / `extends Backbone.View` | UI role | `uiRole: view`, `framework: backbone` | Identifies Backbone views |
| Unrelated receiver's `.fetch()`, `.save()`, or `.destroy()` | None | — | Method name alone does not establish a Backbone model or collection |
