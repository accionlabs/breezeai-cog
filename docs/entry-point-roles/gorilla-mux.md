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
| `r.PathPrefix(prefix).Handler(h)` | Binding | **not recorded yet** | A route on a prefix |
| `r.Use(mw)` | Guard | **not recorded yet** | Runs before many handlers |
