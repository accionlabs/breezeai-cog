# Echo — entry-point roles

Parser: `src/breezeai_cog/parsers/go_echo/` (additive detector, every Go file importing `github.com/labstack/echo`, any major version) · Shared router analysis: `src/breezeai_cog/parsers/go/routing.py` · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_echo.py`

**What is a router.** `echo.New()`, a parameter or struct field declared `*echo.Echo` or
`*echo.Group`, and `e.Group(prefix)`. Rows shared by every Go router (handler location, prefixes
across files, constants, negatives) are in the [Gin table](gin.md) and apply here unchanged.

| Construct | Role | Record | Why |
|---|---|---|---|
| `e.GET(path, h, mw…)` (`GET` `POST` `PUT` `PATCH` `DELETE` `OPTIONS` `HEAD`) | Binding | `route`, the verb, `routeKind: route`, `framework: echo`, `endpoint` = prefix + path, `handler` | One handler |
| `e.Any(path, h)` | Binding | `method: ANY` | Every verb |
| `e.Add("GET", path, h)` | Binding | the verb from the first argument | Verb as an argument |
| `e.CONNECT` / `e.TRACE` | Binding | `method` empty (no contract value) | Verb outside the value set |
| `e.Group(prefix, mw…)` | Wiring | no record; `prefix` joined in front | Points at many handlers |
| `e.Static(prefix, dir)` / `e.File(path, file)` | Binding | `GET`, no handler | Framework-served |
| `e.Use(mw)` / `e.Pre(mw)` | Guard | **not recorded yet** | Runs before many handlers |
| `e.Match([]string{…}, path, h)` | Binding | **not recorded yet** | One record per verb |
