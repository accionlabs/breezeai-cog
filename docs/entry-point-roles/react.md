# React Router — entry-point roles

Parser: `src/breezeai_cog/parsers/typescript_react/` · Rules:
[entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_react.py`

| Construct | Role | Record | Why |
|---|---|---|---|
| `<Route path element={<X/>}>` / `{ path, element: <X/> }` | Binding | `route`, `routeKind: page`, `endpoint` = full path, `handler` = `X` | One component reached by URL |
| `{ path, Component: X }` | Binding | as above, `handler` = `X` | Same, by component reference |
| Element wrapped in elements (`<Suspense><X/></Suspense>`, providers, error boundaries) | Binding | `handler` = the innermost page component `X` | Wrappers are not handlers |
| `const X = lazy(() => import(…))` used as the element | Binding | `handler` = `X` | One component, loaded lazily |
| `<Route path lazy={() => import(…)}>` / `{ path, lazy }` | Binding | `route`, `page`; `handler` = the module's component, empty if unknown | One route module, loaded lazily |
| `<Route index element>` / `{ index: true, element }` | Binding | `endpoint` = the parent's path | Renders at the parent's URL |
| Path-only parent `<Route path>` / `{ path, children }` (no element) | Wiring | statement stays plain; `path` prefixes the children | No handler of its own |
| Element + children with an index (or empty-path) child | Wiring | statement stays plain; prefix for the children | A layout: a child always fills its outlet |
| Element + children with no index child | Binding (and wiring for the children) | `route`, `page`, `handler` = the element | Its own URL shows it with an empty outlet |
| Guard element as a layout (`<Route element={<ProtectedRoute/>}>` + children) | Guard | name in `guards` of each child | Runs before the children's pages |
| `<Route path element={<Navigate to/>}>` | None | — | A redirect; no page |
| v7 `route(path, file)` / `index(file)` | Binding | `route`, `page`; `handler` = the module's component, never the file path | One route module |
| v7 `layout(file, [...])` | Wiring | statement stays plain | A layout around its children |
| v7 `prefix(base, [...])` | Wiring | statement stays plain; `base` prefixes the children | Prefix only |
| `fetch` / `axios` calls | Outbound | `api_call` | |
| `<Link to>` / `<NavLink to>` | Outbound | `semanticType: navigation`, `endpoint` = target path | A link to a page |
| `navigate(path)` from `useNavigate()` | Outbound | `semanticType: navigation`, `endpoint` = target path | Programmatic navigation |
| `createRoot(...).render(<App/>)` / `ReactDOM.render` | None | — | Start-up |
