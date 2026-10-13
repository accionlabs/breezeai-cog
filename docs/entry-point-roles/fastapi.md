# FastAPI — entry-point roles

Parser: `src/breezeai_cog/parsers/python_fastapi/` · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_fastapi.py`

| Construct | Role | Record | Why |
|---|---|---|---|
| `@app.get(path)` / `@router.post(path)` / … on a function | Binding | `route`, HTTP verb, `routeKind: route`, full path, `handler` = function | One handler, inbound API |
| `@router.api_route(path, methods=[…])` | Binding, one per verb | as above, one record per verb (`GET` when `methods` is absent) | One handler per verb |
| `@app.websocket(path)` | Binding | `route`, `method: WEBSOCKET`, `routeKind: ws` | One handler, inbound socket |
| `APIRouter(prefix=…)` | Wiring | statement stays plain; prefix joined into its routes' `endpoint` | Applies to many handlers, no handler of its own |
| `app.include_router(router, prefix=…)` | Wiring | statement stays plain; prefix joined into the router's routes | Points at a router |
| `app.mount(path, sub_app)` | Wiring | statement stays plain | Points at an application, not a handler |
| `dependencies=[Depends(fn)]` on a route, `APIRouter` or `FastAPI` | Guard | `fn` in `guards` of each affected route | Runs before the handler(s) |
| `@app.middleware("http")` | Guard | function name in `guards` of every route of the app | Runs before many handlers |
| `@app.on_event("startup")` / `lifespan` | None | — | Start-up |
| `httpx` / `requests` call | Outbound | `api_call` | Our code calling out |
