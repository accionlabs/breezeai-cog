# AngularJS 1.x — entry-point roles

Parser: `src/breezeai_cog/parsers/typescript_angularjs/` (an additive detector, `angularjs`; it
runs in whichever parser owns the file, so a hybrid AngularJS + Angular file keeps its Angular
owner) · Rules: [entry-point contract](../architecture/entry-points.md) · Tests:
`tests/unit/test_roles_angularjs.py` · `framework: angularjs`

A construct counts only when the code names the AngularJS service itself — `$routeProvider`,
`$stateProvider`, `$urlRouterProvider`, `$resource` — or a registration chain that starts at
`angular.module(…)` (or at a variable assigned from it in the same file). Not followed: renamed injection
parameters, a provider passed into another function, `this.$routeProvider` in a class, and
ES shorthand members (`{ template, controller }`). A `$resource` declared in the file itself is
not the injected service. An address that is not a path (no leading `/`) is left empty.

| Construct | Role | Record | Why |
|---|---|---|---|
| `$routeProvider.when(path, { controller / template / templateUrl })` | Binding | `route`, `routeKind: page`, `endpoint` = path as written (`:id` kept), `handler` = controller name | One view reached by URL |
| `controller: 'Name'` / `'Name as vm'` (and `component: 'name'`) | — | `handler` = `Name` (alias dropped); `handlerPath` / `handlerLine` from the `.controller('Name', …)` registration when it is registered exactly once — its inline function, a function, class or object declared in the file, or (the ES-module layout) the declaration it imports (named as that module declares it) | The handler is named as the route writes it |
| `controller: function () {}` / `['$scope', function () {}]` | — | `handler` empty, `handlerLine` = where the function starts | Anonymous handler (contract §5) |
| `controller: CtrlFn` (a function declared in the file) | — | `handler` = `CtrlFn`, located as any TypeScript handler | Named handler |
| `{ template / templateUrl }` without a controller | Binding | `route`, no handler (a contract gap, not an error) | The address is certain; there is no handler to name. The template file is not linked: its path is relative to the served web root, which the source does not state |
| `.when(path, { redirectTo })`, `.otherwise({ redirectTo })`, `.otherwise('/x')` | Outbound | `route`, `routeKind: navigation`, `endpoint` = the target; empty for a function or a target that isn't a literal | Sends the user to another page; no handler of its own |
| `.otherwise({ templateUrl / controller })` (a catch-all view) | None | — | A wildcard has no address (contract §8: choose the role that records less) |
| `$stateProvider.state(name, { url, controller / component })`, `.state({ name, … })` | Binding | `route`, `routeKind: page`, `endpoint` = the parents' urls + its own url | One state reached by URL |
| A child state (`parent: 'a'`, or a dotted name `a.b`) | Binding | `endpoint` = parent url + own url; a `^` prefix makes the url absolute; a parent that can't be resolved (declared twice differently, or not in the repo) gives an **empty** `endpoint`, never the bare child url | Wiring contributes the prefix |
| `abstract: true` | Wiring | no record; its url prefixes its children | A layout with no page of its own |
| A state with no `url`, or with a `redirectTo` | None | no record; a state with no `url` contributes an empty prefix | No address / names no page of its own |
| `url: '/users?page&sort'` | — | the query declaration is cut: `endpoint` = `/users`; `{id}` and `:id` are kept as written | The address is the path |
| `component: 'userDetail'` | — | `handler` = `userDetail`, located through the `.component('userDetail', …)` registration | Handler named as the state writes it |
| `views: { … }` | Binding | `route`, `page`; `handler` set only when exactly one view names a controller or component, else empty | The address is certain; "one handler" is not |
| `$urlRouterProvider.otherwise('/x')` | Outbound | `route`, `routeKind: navigation`, `endpoint` = `/x`; empty for a function | Default redirect |
| `.controller(name, …)` / `.component(name, …)` / `.directive(name, …)` on `angular.module(…)` | None | a statement with `name`, `uiRole` (`controller`, `component`, `directive`), `framework: angularjs`, no `semanticType` | Declares a UI unit, not an entry point |
| `.service(name, …)` / `.factory(name, …)` | None | nothing (not UI units; Angular's `@Injectable` is likewise left unmarked) | |
| `$resource('/api/users/:id')` | Outbound | `api_call`, `endpoint` = the url (unset when it isn't a literal), `method` unset: one resource declares several verbs | Our code calling out |
| `$http.get(…)` | Outbound | `api_call` (shared classifier; only where the extractor reaches the call) | |
| `angular.module(…).config / .run`, `angular.bootstrap(…)`, `ng-app` | None | — | Start-up |
