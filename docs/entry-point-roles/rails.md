# Rails — entry-point roles

Parser: `src/breezeai_cog/parsers/ruby_rails/` · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_rails.py`

| Construct | Role | Record | Why |
|---|---|---|---|
| `get path, to: "ctrl#action"` / `post` / `put` / `patch` / `delete` inside `Rails.application.routes.draw` | Binding | `route`, HTTP verb, `routeKind: route`, full path (leading `/`), `handler` = action, `handlerLine` = the action's `def`, `handlerPath` = controller file | One action, inbound API |
| `post path => "ctrl#action"` (hash-rocket form) | Binding | as above | Same binding, older syntax |
| `match path, to: …, via: [:get, :post]` | Binding, one per verb | as above, one record per verb | One action per verb |
| `root "ctrl#action"` | Binding | `GET /` | One action |
| `resources :users` / `resource :profile` | Wiring that makes one binding per action | one record per generated route (`GET /users` → `index`, `GET /users/:id` → `show`, …) | Expands to many handlers |
| `namespace :admin` / `scope "/v1"` | Wiring | statement stays plain; prefix joined into nested routes | Applies to many handlers |
| `mount Engine, at: "/x"` | Wiring | statement stays plain | Points at an application |
| `before_action :fn` in a controller | Guard | `fn` in `guards` of the controller's routes | Runs before many actions |
| `Net::HTTP` / `Faraday` / `HTTParty` call | Outbound | `api_call` | Our code calling out |
| Controller action not named by any route | None | — | No address |
