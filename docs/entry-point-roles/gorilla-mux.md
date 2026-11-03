# gorilla/mux — entry-point roles

Parser: `src/breezeai_cog/parsers/go_gorilla/` (additive detector, every Go file importing `github.com/gorilla/mux`) · Shared router analysis: `src/breezeai_cog/parsers/go/routing.py` · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_gorilla_mux.py`

**What is a router.** `mux.NewRouter()`, a parameter or struct field declared `*mux.Router`,
and `r.PathPrefix(prefix).Subrouter()`. Rows shared by every Go router are in the
[Gin table](gin.md).

| Construct | Role | Record | Why |
|---|---|---|---|
| `r.HandleFunc(path, h).Methods("GET", "PUT")` / `r.Handle(path, h).Methods(…)` | Binding per verb | one `route` per verb, `routeKind: route`, `framework: gorilla-mux`, `endpoint` = prefix + path, `handler` | Each verb is served by the handler |
| `.Methods(http.MethodGet)` | Binding | the verb | Same as the literal |
| `r.HandleFunc(path, h)` with no `.Methods` | Binding | empty `method` | Every verb; no verb to record |
| `r.PathPrefix(prefix).Subrouter()` | Wiring | no record; `prefix` joined in front of the subrouter's routes | Points at many handlers |
| `r.Methods(…).Subrouter()` / `r.Host(…).Subrouter()` | Wiring | no record; same prefix | Matchers only |
| `r.PathPrefix(prefix).Handler(h)` / `.HandlerFunc(h)` | Binding | `route` at the prefix, the verbs of `.Methods(…)` on the chain or an empty `method` | A route on a prefix |
| `r.Path(p).Methods("GET").HandlerFunc(h)` | Binding per verb | as `HandleFunc(p, h).Methods("GET")` | `.Methods` may come before the handler |
| `r.Use(mw)` | Guard | `mw` in `guards` of the routes registered on that router after it, and of the groups made from it after it (a call `mw.Auth(cfg)` is named by its function); a router passed in from another file starts with no known guards | Runs before many handlers |
