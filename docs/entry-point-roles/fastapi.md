# FastAPI — entry-point roles

Parser: `src/breezeai_cog/parsers/python_fastapi/` · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_fastapi.py`

| Construct | Role | Record | Why |
|---|---|---|---|
| `@app.get(path)` / `@router.post(path)` / … on a function | Binding | `route`, HTTP verb, `routeKind: route`, full path, `handler` = function | One handler, inbound API |
| `@router.api_route(path, methods=[…])` | Binding, one per verb | as above, one record per verb (`GET` when `methods` is absent) | One handler per verb |
| `@app.websocket(path)` | Binding | `route`, `method: WEBSOCKET`, `routeKind: ws` | One handler, inbound socket |
| `APIRouter(prefix=…)` | Wiring | statement stays plain; prefix joined into its routes' `endpoint` | Applies to many handlers, no handler of its own |
| `app.include_router(router, prefix=…)` | Wiring | statement stays plain; prefix joined into the router's routes. A router included at two or more places (`/v1` and `/v2`) is served at each: its routes' `endpoint` is empty | Points at a router |
| `app.include_router(make_router(), prefix=…)` — a router built by a factory function | Wiring | — (the prefix reaches the routes of the router the factory creates and returns, in this file or an imported one). A router made inside a function that no include reaches has an unknown address: its routes' `endpoint` is empty | Points at a router |
| `app = FastAPI()` — at module level or inside a `create_app()` function | Wiring | — (the app is the root: its routes and the routers it includes are served from `/`) | The application |
| `app.mount("/v1", sub)` — a FastAPI sub-application | Wiring | — (the mount path is not followed: the sub-application's routes have an empty `endpoint`. Matched by name — the variable, the attribute, or the factory that made it — so a same-named app elsewhere is unknown too) | Points at an app |
| `app.mount(path, sub_app)` | Wiring | statement stays plain | Points at an application, not a handler |
| `dependencies=[Depends(fn)]` on a route, `APIRouter` or `FastAPI` | Guard | `fn` in `guards` of each affected route. `authRequired: true` only when `fn` certainly rejects an anonymous call: a `fastapi.security` scheme (`OAuth2PasswordBearer`, `HTTPBearer`, `APIKeyHeader` …) assigned in this file with `auto_error` left on, or a function of this file whose parameter depends on one. Any other dependency (a DB session, paging, a rate limit) or one defined elsewhere makes no claim | Runs before the handler(s) |
| `@app.middleware("http")` | Guard | function name in `guards` of every route of the app | Runs before many handlers |
| `@app.on_event("startup")` / `lifespan` | None | — | Start-up |
| `httpx` / `requests` call | Outbound | `api_call` | Our code calling out |
