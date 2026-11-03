# Grape — entry-point roles

Parser: `src/breezeai_cog/parsers/ruby_grape/` · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_grape.py`

| Construct | Role | Record | Why |
|---|---|---|---|
| `get "path" do … end` / `post :name do … end` / … inside a `Grape::API` class | Binding | `route`, HTTP verb, `routeKind: route`, full path, `handler` empty, `handlerLine` = line of the block | One (anonymous) handler, inbound API |
| `namespace` / `resource` / `resources` / `route_param` | Wiring | statement stays plain; segment joined into nested routes | Applies to many handlers |
| `prefix "api"` / `version "v1"` (path versioning) | Wiring | statement stays plain; joined in front of every route of the class | Applies to many handlers |
| `mount Other::API` | Wiring | statement stays plain | Points at another API |
| `before do … end` / `before_validation` | Guard | in `guards` of each route in scope; `authRequired` stays empty — a hook may set a header or a locale as well as authenticate | Runs before many handlers |
| `desc` / `params do … end` | None | — | Documentation / validation of the next route |
