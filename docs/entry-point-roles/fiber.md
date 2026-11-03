# Fiber — entry-point roles

Parser: `src/breezeai_cog/parsers/go_fiber/` (additive detector, every Go file importing `github.com/gofiber/fiber`, any major version) · Shared router analysis: `src/breezeai_cog/parsers/go/routing.py` · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_fiber.py`

**What is a router.** `fiber.New()`, a parameter or struct field declared `*fiber.App`,
`fiber.Router` or `*fiber.Group`, `app.Group(prefix)`, and the router a `Route` closure receives.
Rows shared by every Go router are in the [Gin table](gin.md).

| Construct | Role | Record | Why |
|---|---|---|---|
| `app.Get(path, h)` (`Get` `Post` `Put` `Patch` `Delete` `Options` `Head`) | Binding | `route`, the verb, `routeKind: route`, `framework: fiber`, `endpoint` = prefix + path, `handler` = the last argument | One handler |
| `app.All(path, h)` | Binding | `method: ANY` | Every verb |
| `app.Add("GET", path, h)` (v2) | Binding | the verb from the first argument | Verb as an argument |
| `app.Group(prefix, mw…)` | Wiring | no record; `prefix` joined in front | Points at many handlers |
| `app.Route(prefix, func(r fiber.Router) {…})` | Wiring | no record; `prefix` joined in front of the closure's routes | Points at many handlers |
| `app.Mount(prefix, sub)` | Wiring | no record; `sub`'s routes get `prefix` | Mounts a sub-app |
| `app.Static(prefix, dir)` | Binding | `GET`, no handler | Framework-served |
| `app.Use(mw)` | Guard | `mw` in `guards` of the routes registered on that router after it, and of the groups made from it after it (a call `mw.Auth(cfg)` is named by its function); a router passed in from another file starts with no known guards | Runs before many handlers |
| `app.Use("/prefix", mw)` | Guard | **not recorded** — scoped to a path | Runs before the routes under the path |
| Middleware before the handler (`app.Get(p, auth, h)`) and a group's (`app.Group(p, auth)`) | Guard | their names in `guards`, outermost first | Run before this handler |
