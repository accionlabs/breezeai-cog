# Gin — entry-point roles

Parser: `src/breezeai_cog/parsers/go_gin/` (additive detector, every Go file importing `github.com/gin-gonic/gin`) · Shared router analysis: `src/breezeai_cog/parsers/go/routing.py` · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_gin.py`

**What is a router.** cog decides from how a name is bound, never from the name alone:

- a constructor: `gin.New()`, `gin.Default()`;
- a parameter or a struct field declared `*gin.Engine`, `*gin.RouterGroup`, `gin.IRouter` or
  `gin.IRoutes` (a struct declared in another file of the package counts too);
- a group: `r.Group("/v1")`.

**Where a router is served.** A constructed router is served at the root. A router parameter is
served wherever the routers passed to it are: every call `F(router)` in the repo is followed
(`gin.router-prefixes`), across files and packages. All Go routers ([net/http](net-http.md),
[Echo](echo.md), [chi](chi.md), [Fiber](fiber.md), [gorilla/mux](gorilla-mux.md)) share this
analysis, so the rows marked **(shared)** are tested here once.

| Construct | Role | Record | Why |
|---|---|---|---|
| `r.GET(path, handler)` (`GET` `POST` `PUT` `PATCH` `DELETE` `OPTIONS` `HEAD`) with a function declared in this file | Binding | `route`, the verb, `routeKind: route`, `framework: gin`, `endpoint` = the router's prefix + `path`, `handler` = the name, `handlerLine` = its declaration | One handler, inbound API |
| Same, with a function literal | Binding | as above, no `handler`; `handlerLine` = where the literal starts | Anonymous handler |
| Same, with middleware before the handler (`r.GET(p, auth, h)`) | Binding | `handler` = the last argument | Gin runs the handlers in order; the last one answers |
| **(shared)** A handler declared in another file of the package, or `pkg.Handler` of another package of the repo | Binding | `handlerLine` + `handlerPath` | Located through `go.declarations` |
| **(shared)** A method value `h.List` where `h` is a parameter or `h := &Handler{…}` | Binding | `handler: h.List`, located on `Handler.List` | The receiver's type is known |
| **(shared)** A method value whose receiver's type is unknown | Binding | located when exactly one method of that name exists in the repo, else `handlerLine` empty | Never a guessed declaration |
| **(shared)** A wrapped handler (`gin.WrapF(h)`, `mw(h)`) | Binding | `handler` = the innermost last argument | The wrapper only forwards |
| `r.Any(path, h)` | Binding | `method: ANY` | Every verb |
| `r.Handle("GET", path, h)` | Binding | the verb from the first argument; empty `method` when it isn't a literal or a constant | Verb as an argument |
| `r.Group(prefix)` | Wiring | no record; `prefix` joined in front of the group's routes | Points at many handlers |
| **(shared)** `Register(r.Group("/users"))` — a router passed to a function of the repo (any file, any package) | Wiring | no record; the function's routes get the prefix | It runs on that router |
| **(shared)** A function given routers under two different prefixes, or a router whose prefix is unknown | Wiring | its routes have an empty `endpoint` | Never a partial or guessed address |
| **(shared)** A router parameter nothing in the repo passes a router to | Binding | `endpoint` = the route's own path | Served at the root unless a call says otherwise |
| **(shared)** A struct field of the app type (`router *gin.Engine`) | Binding | served at the root | The field holds the app |
| **(shared)** A struct field of a group type (`api *gin.RouterGroup`) | Binding | `endpoint` empty | Where the group was made is not followed |
| **(shared)** Path from a constant of the file (`const base = "/x"`, `base + "/y"`) | Binding | the folded path | Constant folding |
| **(shared)** Path from a variable or a call | Binding | `endpoint` empty | Never a guessed address |
| `r.Static(prefix, dir)` / `StaticFS` / `StaticFile` | Binding | `route`, `GET`, `endpoint` = prefix, no handler | Framework-served files |
| `r.Use(mw)` | Guard | `mw` in `guards` of the routes registered on that router after it, and of the groups made from it after it (a call `mw.Auth(cfg)` is named by its function); a router passed in from another file starts with no known guards | Runs before many handlers |
| Middleware before the handler (`r.GET(p, auth, h)`) and a group's (`r.Group(p, auth)`) | Guard | their names in `guards`, outermost first | Run before this handler |
| `r.NoRoute(h)` / `r.NoMethod(h)` / `r.Run(addr)` | None | — | Not an address of their own |
| **(shared)** `client.GET(url)` on a value that is not a Gin router | None (Outbound if an HTTP client) | — | Not a router: bound from no Gin constructor, type or group |
| **(shared)** A route in a fixture file (`mock`, `fixtures` folder) | None | — | Not served |
