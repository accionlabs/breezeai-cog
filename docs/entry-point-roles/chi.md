# chi — entry-point roles

Parser: `src/breezeai_cog/parsers/go_chi/` (additive detector, every Go file importing `github.com/go-chi/chi`, any major version) · Shared router analysis: `src/breezeai_cog/parsers/go/routing.py` · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_chi.py`

**What is a router.** `chi.NewRouter()` / `chi.NewMux()`, a parameter or struct field declared
`chi.Router`, `*chi.Mux` or `chi.Routes`, the router a `Route` / `Group` closure receives, and
`r.With(mw…)`. Rows shared by every Go router are in the [Gin table](gin.md).

| Construct | Role | Record | Why |
|---|---|---|---|
| `r.Get(path, h)` (`Get` `Post` `Put` `Patch` `Delete` `Options` `Head`) | Binding | `route`, the verb, `routeKind: route`, `framework: chi`, `endpoint` = prefix + path, `handler` | One handler |
| `r.Handle(path, h)` / `r.HandleFunc(path, h)` | Binding | empty `method` | Every verb; no verb to record |
| `r.Method("GET", path, h)` / `r.MethodFunc(…)` | Binding | the verb from the first argument | Verb as an argument |
| `r.With(mw).Get(path, h)` | Binding | as `r.Get` | `With` keeps the prefix |
| `r.Route(prefix, func(r chi.Router) {…})` | Wiring | no record; `prefix` joined in front of the closure's routes | Points at many handlers |
| `r.Group(func(r chi.Router) {…})` | Wiring | no record; same prefix | Groups middleware only |
| `r.Mount(prefix, sub)` — `sub` built in this function, or `subRouter()` returning a router it builds | Wiring | no record; `sub`'s routes get `prefix` (across files through `chi.router-prefixes`) | Mounts a router |
| A router mounted twice | Wiring | its routes have an empty `endpoint` | Never a partial or guessed address |
| `r.Use(mw)` | Guard | **not recorded yet** | Runs before many handlers |
| `r.NotFound(h)` / `r.MethodNotAllowed(h)` | None | — | Not an address of their own |
