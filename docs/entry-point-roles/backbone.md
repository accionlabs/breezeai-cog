# Backbone — entry-point roles

Parser: `src/breezeai_cog/parsers/typescript_backbone/` (additive detectors: `backbone.py` for API calls and views, `routes.py` for routes; eligible TS / JS files) ·
Rules: [entry-point contract](../architecture/entry-points.md) ·
Tests: `tests/unit/test_roles_backbone.py`

`routes.py` uses the gate-level `b"oute"` byte guard and skips fixture files. Since Marionette-only files may not mention "Backbone", the detector also checks for Backbone/Marionette markers before building the shared class scope. The detectors share a cached class scope for each file, keeping scope construction to one pass per file when both detectors run.

| Construct | Role | Record | Why |
|---|---|---|---|
| `Backbone.Router.extend({ routes: { path: handler } })` / `extends Backbone.Router` with a literal `routes` table | Binding | `route`, `routeKind: page`, `framework: backbone`, `endpoint` = route path joined to a literal `Backbone.history.start({ root })`, `handler` = route handler | A client-side page reached by URL |
| `router.route(path, name, callback)` / `router.route(path, name)` | Binding | `route`, `routeKind: page`; literal path and resolvable handler are captured | Runtime route registration |
| `router.route(/pattern/, name)` with a RegExp literal | Binding | `route`, `routeKind: page`, `isRegex: true`; `endpoint` is the literal RegExp source | The literal pattern is the address Backbone matches against the URL fragment |
| A route path that does not resolve to a literal (computed path or computed RegExp) in a route map or `router.route(...)` | None | — | No concrete route path is available |
| `routes` / Marionette `appRoutes` given as a variable, with `...spread` of known objects, computed `[CONST]` keys, `new Router({ routes })`, or `router.route` through an alias / `this` | Binding | Same record per entry, at the entry; each router keeps its own handlers (`appRoutes` handlers come from `controller`) | Statically resolvable tables are still bindings |
| `Backbone.history.start({ root })` without a literal `pushState: true` | None | Root is not applied to endpoints | A hash-based router (the default) lives after `#`, not under the root |
| A non-literal history root | Binding | Routes keep their literal paths without a base prefix | Never guess a base path |
| `save()` where `isNew()` is not settled by the constructor (variable argument, spread, unknown `idAttribute`), or `url()` computed at runtime | Outbound | `api_call` with an empty `method` (save) or empty `endpoint` (computed `url`); `save(…, { patch: true })` is `PATCH` | Backbone decides at runtime; never guess |
| A receiver bound to different classes in one file | None | — | A name is not a type; ambiguous names resolve to nothing |
| `express.Router()`, `UserViewModel`, or any base class not from `backbone` / `backbone.marionette` | None | — | Only library-rooted classes are Backbone kinds |
| Model `fetch()` / `save()` / `destroy()` | Outbound | `api_call`; `GET` / `PUT` or `POST` / `DELETE`; literal `url` or `urlRoot` used as endpoint where resolvable | A model request to the server |
| Collection `fetch()` / `create()` | Outbound | `api_call`; `GET` / `POST`; literal `url` used as endpoint where resolvable | A collection request to the server |
| `Backbone.sync(method, modelOrCollection)` | Outbound | `api_call`; method mapped from Backbone's `read`, `create`, `update`, `patch`, or `delete` operation | Explicit sync request |
| A model or collection endpoint that cannot be resolved to a literal | Outbound | `api_call` with a null endpoint | Do not stringify a variable or expression |
| `Backbone.View.extend(...)` / `extends Backbone.View` | UI role | `uiRole: view`, `framework: backbone` | Identifies Backbone views |
| Unrelated receiver's `.fetch()`, `.save()`, or `.destroy()` | None | — | Method name alone does not establish a Backbone model or collection |
