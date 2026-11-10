# Angular — entry-point roles

Parser: `src/breezeai_cog/parsers/typescript_angular/` (route configs), `parsers/html/statements.py`
(`routerLink` in templates) · Rules: [entry-point contract](../architecture/entry-points.md) ·
Tests: `tests/unit/test_roles_angular.py`

| Construct | Role | Record | Why |
|---|---|---|---|
| `{ path, component }` in a `Routes` array / `RouterModule.forRoot` / `forChild` | Binding | `route`, `routeKind: page`, `endpoint` = full path, `handler` = component | One component reached by URL |
| `{ path, loadComponent: () => import(…).then(m => m.X) }` | Binding | as above, `handler` = `X` | One component, loaded lazily |
| `{ path, loadComponent: () => import('./x') }` (default export) | Binding | as above, `handler` = the component name, never the import path | Same; the file goes in `handlerPath` |
| `{ path, loadChildren }` | Wiring | no record when its loaded routes are in the repo and get its prefix: `path` prefixes the loaded module's routes — reached through the module class (`.then(m => m.X)`) or, for a standalone routes file (`import('./admin/admin.routes')`), through the file — and the guards that apply to the mount route (its ancestors', its own `canActivate` / `canMatch`, its `canActivateChild`) go first in their `guards`. A target mounted at two prefixes, or under a path that can't be resolved, gives its routes a null `endpoint`, never their bare path. A mount whose loaded routes aren't reached (a module outside the repo, an ambiguous or unresolved prefix) keeps a `route`, `routeKind: mount` record (the Target Spec's current form; not an entry point), `handler` = the loaded module / routes symbol, empty for a bare import path | Points at a module or route list |
| `{ path, children: [...] }` (no component) | Wiring | no record; `path` prefixes the children | No handler of its own |
| `{ path, component, children }` with a child `path: ''` (a redirect too) | Wiring | no record; prefix for the children | A layout: a child always fills its outlet |
| `{ path, component, children }` with no empty-path child | Binding (and wiring for the children) | `route`, `page`, `handler` = component | Its own URL shows it with an empty outlet |
| `{ path, redirectTo }` | Outbound | `route`, `routeKind: navigation`, `endpoint` = target path — an absolute target (`/x`) as-is, a relative one joined onto the parent path, either without its query string or fragment; empty for a function form or a target that can't be resolved | Sends the user to another page; no handler of its own |
| `canActivate` / `canMatch` on a route | Guard | names in `guards` of that route | Run before its handler |
| `canActivateChild` on a parent | Guard | names in `guards` of each child | Run before the children's handlers |
| `HttpClient` calls (`this.http.get(…)`) | Outbound | `api_call` | |
| `routerLink` / `[routerLink]` in a template | Outbound | `route`, `routeKind: navigation`, `endpoint` = target path | A link to a page |
| `router.navigate([...])` / `router.navigateByUrl(...)` | Outbound | `route`, `routeKind: navigation`, `endpoint` = target path | Programmatic navigation |
| `bootstrapApplication(...)` / `platformBrowserDynamic().bootstrapModule(...)` | None | — | Start-up |
