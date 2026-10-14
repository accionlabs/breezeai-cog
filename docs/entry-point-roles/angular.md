# Angular — entry-point roles

Parser: `src/breezeai_cog/parsers/typescript_angular/` (route configs), `parsers/html/statements.py`
(`routerLink` in templates) · Rules: [entry-point contract](../architecture/entry-points.md) ·
Tests: `tests/unit/test_roles_angular.py`

| Construct | Role | Record | Why |
|---|---|---|---|
| `{ path, component }` in a `Routes` array / `RouterModule.forRoot` / `forChild` | Binding | `route`, `routeKind: page`, `endpoint` = full path, `handler` = component | One component reached by URL |
| `{ path, loadComponent: () => import(…).then(m => m.X) }` | Binding | as above, `handler` = `X` | One component, loaded lazily |
| `{ path, loadComponent: () => import('./x') }` (default export) | Binding | as above, `handler` = the component name, never the import path | Same; the file goes in `handlerPath` |
| `{ path, loadChildren }` | Wiring | `route`, `routeKind: mount` (the Target Spec's current form; not an entry point), `handler` = the loaded module / routes; `path` prefixes the loaded module's routes, and the guards that apply to the mount route (its ancestors', its own `canActivate` / `canMatch`, its `canActivateChild`) go first in their `guards` | Points at a module or route list |
| `{ path, children: [...] }` (no component) | Wiring | `route`, `routeKind: page`, no handler (the Target Spec's current form); `path` prefixes the children | No handler of its own |
| `{ path, component, children }` with a child `path: ''` | Wiring | `route`, `routeKind: page`, `handler` = the component (the Target Spec's current form); prefix for the children | A layout: a child always fills its outlet |
| `{ path, component, children }` with no empty-path child | Binding (and wiring for the children) | `route`, `page`, `handler` = component | Its own URL shows it with an empty outlet |
| `{ path, redirectTo }` | None | — | An alias; no handler |
| `canActivate` / `canMatch` on a route | Guard | names in `guards` of that route | Run before its handler |
| `canActivateChild` on a parent | Guard | names in `guards` of each child | Run before the children's handlers |
| `HttpClient` calls (`this.http.get(…)`) | Outbound | `api_call` | |
| `routerLink` / `[routerLink]` in a template | Outbound | `route`, `routeKind: navigation`, `endpoint` = target path | A link to a page |
| `router.navigate([...])` / `router.navigateByUrl(...)` | Outbound | `route`, `routeKind: navigation`, `endpoint` = target path | Programmatic navigation |
| `bootstrapApplication(...)` / `platformBrowserDynamic().bootstrapModule(...)` | None | — | Start-up |
