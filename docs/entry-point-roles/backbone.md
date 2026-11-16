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
| `Backbone.history.start({ root })` without a literal `pushState: true`, or `start({ pushState: true })` without `root` | None | Root is not applied to endpoints | A hash-based router (the default) lives after `#`, not under the root |
| A non-literal history root | Binding | Routes keep their literal paths without a base prefix | Never guess a base path |
| A file with no `history.start` of its own | Binding | Its routes take the root only when **every** `history.start` of the repo has that same root; otherwise no prefix | Two apps in one repo must not share one app's root |
| A route handler named as a string (`'orders/:id': 'show'`) | Binding | `handlerLine` = that router's own `show`, else empty — never a same-named function elsewhere in the file | The name is a method of the router, not a symbol in scope |
| A route handler given as a function (`'orders/:id': showOrder`) | Binding | `handlerLine` (and `handlerPath`) where the function is declared, in the file or through its import | The value is a reference to that function |
| `save()` where `isNew()` is not settled by the constructor (variable argument, spread, unknown `idAttribute`, a class whose `defaults` hold the id or are not a plain object, an `initialize` / `constructor` that calls `set(…)` or names the id), or `url()` computed at runtime | Outbound | `api_call` with an empty `method` (save) or empty `endpoint` (computed `url`) | Backbone decides at runtime; never guess |
| `save(…, { patch: true })` | Outbound | `PATCH` for a model that has an id, `POST` for a new one (Backbone creates it whatever `patch` says), empty when not known | `isNew() ? create : patch` |
| A model's `fetch()` when `isNew()` is not known | Outbound | `GET` with an empty `endpoint` | The id is in the address only for a model that has one |
| A receiver bound to different classes in one file | None | — | A name is not a type; ambiguous names resolve to nothing |
| `express.Router()`, `UserViewModel`, or any base class not from `backbone` / `backbone.marionette` | None | — | Only library-rooted classes are Backbone kinds |
| Model `fetch()` / `save()` / `destroy()` | Outbound | `api_call`; `GET` / `PUT` or `POST` / `DELETE`; endpoint = a literal `url`, or a literal `urlRoot` — followed by `/{id}` for a model with an id, as other `api_call`s write a path parameter | A model request to the server |
| Collection `fetch()` / `create()` | Outbound | `api_call`; `GET` / `POST`; literal `url` used as endpoint where resolvable | A collection request to the server |
| `Backbone.sync(method, modelOrCollection)` | Outbound | `api_call`; method mapped from Backbone's `read`, `create`, `update`, `patch`, or `delete` operation | Explicit sync request |
| A model or collection endpoint that cannot be resolved to a literal | Outbound | `api_call` with a null endpoint | Do not stringify a variable or expression |
| `Backbone.View.extend(...)` / `extends Backbone.View` (also Marionette views) | UI role | `uiRole: component`, `framework: backbone` | A view is the app's UI component |
| Unrelated receiver's `.fetch()`, `.save()`, or `.destroy()` | None | — | Method name alone does not establish a Backbone model or collection |
| `new Todo(…).save()` where `Todo` is declared in this file (`const Todo = mongoose.model(…)`), or — in a file that imports — not imported, while another file declares a Backbone `Todo` | None | — | The other file's class is not the one this file means |
