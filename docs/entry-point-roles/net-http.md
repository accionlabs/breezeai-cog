# net/http — entry-point roles

Parser: `src/breezeai_cog/parsers/go_nethttp/` (additive detector, every Go file importing `net/http`) · Shared router analysis: `src/breezeai_cog/parsers/go/routing.py` · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_net_http.py`

**What is a router.** `http.NewServeMux()`, a parameter or struct field declared
`*http.ServeMux`, and the package's default mux (`http.HandleFunc`, `http.Handle`). Rows shared
by every Go router (handler location, prefixes across files, constants, negatives) are in the
[Gin table](gin.md) and apply here unchanged.

| Construct | Role | Record | Why |
|---|---|---|---|
| `mux.HandleFunc(pattern, h)` / `mux.Handle(pattern, h)` with a pattern without a verb | Binding | `route`, empty `method`, `routeKind: route`, `framework: net-http`, `endpoint` = the pattern, `handler` | Serves every verb; no verb to record |
| Same with a Go 1.22 verb (`"GET /items/{id}"`) | Binding | `method: GET`, `endpoint: /items/{id}` | The verb is part of the pattern |
| A pattern with a host (`"example.com/x"`) | Binding | `endpoint: /x` | The host is not part of the path |
| `http.HandleFunc(pattern, h)` / `http.Handle(pattern, h)` | Binding | as above | The default mux |
| `mux.Handle(p, http.HandlerFunc(h))` / `mux.Handle(p, mw(h))` | Binding | `handler: h` | The wrapper only forwards |
| `mux.Handle(p, http.FileServer(…))` / `expvar.Handler()` | Binding | no `handler` | Framework-served |
| `mux.Handle("/api/", http.StripPrefix("/api", sub))` | Wiring | `sub`'s routes should get `/api` — **not followed yet**; they keep their own paths | Mount |
| `http.ListenAndServe(addr, mux)` / `http.Get(url)` | None / Outbound | `http.Get` is an `api_call` | Not a route |
| A pattern from a variable or `fmt.Sprintf` (an in-repo wrapper that registers routes for its callers) | Binding | `endpoint` empty | The wrapper's callers are not followed (known limit) |
