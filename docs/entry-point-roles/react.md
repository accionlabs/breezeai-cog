# React Router — entry-point roles

Parser: `src/breezeai_cog/parsers/typescript_react/` · Rules:
[entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_react.py`

Routes are the additive `react-routes` detector: a router file another parser owns (one importing
a `@nestjs/…` DTO in a monorepo) keeps its routes.

| Construct | Role | Record | Why |
|---|---|---|---|
| `<Route path element={<X/>}>` / `{ path, element: <X/> }` | Binding | `route`, `routeKind: page`, `endpoint` = full path, `handler` = `X`; a route-config entry has `nodeType: object`, its source as `text` | One component reached by URL |
| `{ path, Component: X }` | Binding | as above, `handler` = `X` | Same, by component reference |
| Element wrapped in elements (`<Suspense><X/></Suspense>`, providers, error boundaries) | Binding | `handler` = the innermost page component `X` | Wrappers are not handlers |
| `const X = lazy(() => import(…))` used as the element | Binding | `handler` = `X` | One component, loaded lazily |
| `<Route path lazy={() => import(…)}>` / `{ path, lazy }` | Binding | `route`, `page`; `handler` = the module's component, empty if unknown | One route module, loaded lazily |
| `<Route index element>` / `{ index: true, element }` | Binding | `endpoint` = the parent's path | Renders at the parent's URL |
| Path-only parent `<Route path>` / `{ path, children }` (no element) | Wiring | statement stays plain; `path` prefixes the children | No handler of its own |
| Element + children with an index (or empty-path) child | Wiring | statement stays plain; prefix for the children | A layout: a child always fills its outlet |
| Element + children with no index child | Binding (and wiring for the children) | `route`, `page`, `handler` = the element | Its own URL shows it with an empty outlet |
| Guard element as a layout (`<Route element={<ProtectedRoute/>}>` + children) | Guard | name in `guards` of each child. A guard by whole words of its name: `Protected`, `Private`, `Guard`, `Authenticated`, `Authorized` (not after `Non`/`Not`/`No`), or `Require` + `Auth`/`Login`/`User`/`Role`/`Admin`/`Permission`/`Session`; not a name with `Layout`/`Page`/`Shell` (`RequiredFieldsNotice`, `UnprotectedRoute` are not guards) | Runs before the children's pages |
| `<Route path element={<Navigate to/>}>` | None | — | A redirect; no page |
| Router key written as a method or shorthand (`{ path: '/', loader() {…}, Component() {…} }`, `{ path, Component }`) | Binding | `route`, `page`; `handler` empty (an inline or shorthand component has no reference to read) | Counted like `key: value` router keys |
| Nested path starting with `/` (`/p/kid` under `/p`) | Binding | `endpoint` = the path as written, not joined onto the parent | React Router requires an absolute child to repeat its parents' path |
| `path` a constant (`PATHS.home`, `HOME`, a `${…}` template of consts), in this file or imported | Binding | `endpoint` = the constant's value | Folded through the repo-wide constants, as for v7 `route()` |
| `path` not a literal and not a constant that folds (`undefined`, `'/a/' + id`, a missing key, shorthand `path`) | Binding | `endpoint` empty, and the relative paths under it too | The address is unknown; never the expression's text |
| `{ path, … }` arrays in a file that never mentions `react-router` and whose lists no React Router call takes (a component's tab or menu list, a Vue router) | None | — | Not router config; when unsure, record less |
| A route list split into a file without the import, that a React Router call takes through imports: passed in (`createBrowserRouter(routes)`, `useRoutes(routes)`), spread in, as a pathless layout's `children`, or through a re-exporting barrel | Binding (in the list's file) | as a route in the router file | Followed through any chain of imports, tsconfig/Vite aliases and `require` |
| A split list mounted as the `children` of a route with a path (`{ path: '/admin', children: adminChildren }`) | None | — | Its paths hang off a parent its file doesn't know; never a guessed URL |
| v7 `route(path, file)` / `index(file)` | Binding | `route`, `page`; `handler` = the module's component, never the file path | One route module |
| v7 `layout(file, [...])` | Wiring | statement stays plain | A layout around its children |
| v7 `prefix(base, [...])` | Wiring | statement stays plain; `base` prefixes the children | Prefix only |
| `fetch` / `axios` calls | Outbound | `api_call` | |
| `<Link to>` / `<NavLink to>` | Outbound | `route`, `routeKind: navigation`, `endpoint` = target path | A link to a page |
| `navigate(path)` from `useNavigate()` | Outbound | `route`, `routeKind: navigation`, `endpoint` = target path | Programmatic navigation |
| `createRoot(...).render(<App/>)` / `ReactDOM.render` | None | — | Start-up |
