# Express — entry-point roles

Parser: `src/breezeai_cog/parsers/typescript_express/` (additive detector, every TS / JS file that imports `express`) · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_express.py`

| Construct | Role | Record | Why |
|---|---|---|---|
| `app.METHOD(path, …, handler)` / `router.METHOD(path, …, handler)` with a named handler | Binding | `route`, HTTP verb, `routeKind: route`, full path as `endpoint`, `handler` = the name, `handlerLine` = its declaration | One handler, inbound API |
| Same, with an inline function as the handler | Binding | as above; `handler` empty, `handlerLine` = where the function starts | Anonymous handler |
| Same, with a wrapper call as the handler (`asyncHandler(async (req, res) => …)`) | Binding | as above; the handler is the innermost function | Wrappers are not handlers |
| `router.route(path).get(h1).post(h2)` | Binding per verb | one `route` per verb call, `endpoint` = `path` | Each verb call names one handler; `route(path)` itself only adds the path |
| `.all(mw)` in a `route(path)` chain, before other verbs | Guard | `mw` in `guards` of each verb chained after it, no record of its own; `authRequired` not set (like other middleware that runs first). Last in the chain or alone, `.all(h)` is a catch-all: `route`, `ANY` | Runs before every verb after it |
| Functions between the path and the handler | Guard | their names in `guards`; `authRequired: true` only for an [express-jwt](https://github.com/auth0/express-jwt) `expressjwt({…})` that requires a token (no `credentialsRequired: false`, no `.unless(…)`), called there or bound to a variable of this file. Any other middleware — validation, logging, the app's own `requireAuth` — makes no claim | Run before this handler |
| `app.use(path, router)` / `app.use(path, routerFactory())` | Wiring | statement stays plain when the router is confirmed — a `Router()` of this file, or another file's router or factory that `express.router-mounts` resolves — and keeps its base: `path` is joined in front of the router's bindings, and the mount's own middleware (`app.use(path, auth, router)`) go first in their `guards`, in this file or the router's file. Any other router (from a package, ambiguous, mounted at two bases) keeps a `route`, `routeKind: mount`, `ANY` record (the Target Spec's current form; not an entry point): `endpoint` = the full prefix, `handler` = the router, `guards` = its middleware | Points at a router, no handler |
| `app.use(fn)` / `app.use(path, fn)` where `fn` is middleware | Guard | `fn` in `guards` of the bindings registered after it | Runs before many handlers |
| `app.use(path, mw)` where `mw` is framework middleware that serves requests: `swagger-ui-express` `serve` / `setup`, `express.static`, `serve-static`, `express-graphql` `graphqlHTTP`, `graphql-http` `createHandler` | Binding | `route`, `ANY`, `routeKind: route`, `endpoint` = the path, no handler (framework-served); `framework: graphql` for the GraphQL handlers | Requests to the path are answered there |
| `app.use(path, expressMiddleware(server))` (Apollo) | Wiring | `route`, `POST`, `endpoint` = the path (else `/graphql`), `framework: graphql` — the Target Spec's current form | GraphQL transport; the operations are the bindings |
| Route path with an unresolved part (`` `${BASE}/users` `` with `BASE` from config) | Binding | `endpoint` empty | Never a partial or guessed address |
| `app.get('title')` (settings getter: one string argument) | None | — | Not a route |
| `app.listen(port)` | None | — | Start-up |
| `fetch(...)` / `axios.get(...)` | Outbound | `api_call` | Our code calling out |
