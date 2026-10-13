# Sinatra — entry-point roles

Parser: `src/breezeai_cog/parsers/ruby_sinatra/` · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_sinatra.py`

| Construct | Role | Record | Why |
|---|---|---|---|
| `get "/path" do … end` / `post` / `put` / `patch` / `delete` / … | Binding | `route`, HTTP verb, `routeKind: route`, full path, `handler` empty, `handlerLine` = line of the block | One (anonymous) handler, inbound API |
| `namespace "/api" do … end` (sinatra-contrib) | Wiring | statement stays plain; prefix joined into nested routes | Applies to many handlers |
| `before do … end` / `before "/path" do … end` | Guard | in `guards` of each affected route | Runs before many handlers |
| `use Rack::Middleware` | Guard | middleware name in `guards` | Runs before many handlers |
| `run!` / `set :port` | None | — | Start-up |
