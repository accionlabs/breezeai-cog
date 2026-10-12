# Express — entry-point roles

Parser: `src/breezeai_cog/parsers/typescript_express/` (additive detector, every TS / JS file that imports `express`) · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_express.py`

| Construct | Role | Record | Why |
|---|---|---|---|
| `app.METHOD(path, …, handler)` / `router.METHOD(path, …, handler)` with a named handler | Binding | `route`, HTTP verb, `routeKind: route`, full path as `endpoint`, `handler` = the name, `handlerLine` = its declaration | One handler, inbound API |
| Same, with an inline function as the handler | Binding | as above; `handler` empty, `handlerLine` = where the function starts | Anonymous handler |
| Same, with a wrapper call as the handler (`asyncHandler(async (req, res) => …)`) | Binding | as above; the handler is the innermost function | Wrappers are not handlers |
| `router.route(path).get(h1).post(h2)` | Binding per verb | one `route` per verb call, `endpoint` = `path` | Each verb call names one handler; `route(path)` itself only adds the path |
| Functions between the path and the handler | Guard | their names in `guards` | Run before this handler |
| `app.use(path, router)` / `app.use(path, routerFactory())` | Wiring | statement stays plain; `path` is joined in front of the router's bindings, in this file or the router's file | Points at a router, no handler |
| `app.use(fn)` / `app.use(path, fn)` where `fn` is middleware | Guard | `fn` in `guards` of the bindings registered after it | Runs before many handlers |
| `app.use(path, x)` where `x` may be a router or middleware (`express.static(...)`) | Wiring | statement stays plain | Contract §8: choose the role that records less |
| `app.use(path, expressMiddleware(server))` (Apollo) | Wiring | statement stays plain | GraphQL transport; the operations are the bindings |
| Route path with an unresolved part (`` `${BASE}/users` `` with `BASE` from config) | Binding | `endpoint` empty | Never a partial or guessed address |
| `app.get('title')` (settings getter: one string argument) | None | — | Not a route |
| `app.listen(port)` | None | — | Start-up |
| `fetch(...)` / `axios.get(...)` | Outbound | `api_call` | Our code calling out |
