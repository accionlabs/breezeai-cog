# Rails — entry-point roles

Parser: `src/breezeai_cog/parsers/ruby_rails/` · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_rails.py`

| Construct | Role | Record | Why |
|---|---|---|---|
| `get path, to: "ctrl#action"` / `post` / `put` / `patch` / `delete` inside `Rails.application.routes.draw`, or `<App>::Application.routes.draw` (Rails 3 and 4) | Binding | `route`, HTTP verb, `routeKind: route`, full path (leading `/`), `handler` = action, `handlerLine` = the action's `def`, `handlerPath` = controller file | One action, inbound API |
| `post path => "ctrl#action"` (hash-rocket form) | Binding | as above | Same binding, older syntax |
| `match path, to: …, via: [:get, :post]` | Binding, one per verb | as above, one record per verb | One action per verb |
| `root "ctrl#action"` | Binding | `GET /` | One action |
| `resources :users` / `resource :profile` | Wiring that makes one binding per action | one record per generated route (`GET /users` → `index`, `GET /users/:id` → `show`, …) | Expands to many handlers |
| `namespace :admin` / `scope "/v1"` | Wiring | statement stays plain; prefix joined into nested routes | Applies to many handlers |
| `mount Engine, at: "/x"` | Wiring | statement stays plain | Points at an application |
| `before_action :fn` in a controller (also `before_filter`, `append_before_action` / `_filter`) | Guard | `fn` in `guards` of the controller's routes, limited by literal `only:` / `except:`; a callback with `if:` / `unless:`, a block or a non-symbol argument adds nothing | Runs before many actions |
| `prepend_before_action :fn` (also `prepend_before_filter`) | Guard | `fn` at the front of `guards`, ahead of inherited callbacks | Rails runs it first |
| Callbacks of a parent controller (`class UsersController < ApplicationController`) | Guard | inherited across files down the `< Parent` chain, parent resolved the way Ruby resolves the constant; a parent that is not a controller in the repo, or is declared ambiguously, ends the chain | Runs before the child's actions |
| `skip_before_action :fn` (`only:` / `except:`; also `skip_before_filter`) | Guard removal | `fn` leaves `guards` of the actions it names (`only:`), or of every other action (`except:`); inherited callbacks included. A skip with `if:` / `unless:` or an action list that isn't literal removes `fn` everywhere | A guard is never kept where it may be skipped |
| Devise `authenticate :scope do … end` / `authenticated :scope do … end` around routes | Guard | `authenticate(:scope)` / `authenticated(:scope)` in `guards` of the nested routes, outermost first; `unauthenticated` blocks add nothing | Only a signed-in user reaches them |
| A guard that certainly rejects anonymous users | — | `authRequired: true` on the route; otherwise `authRequired` is left empty. These count: Devise `authenticate_<scope>!`, the Devise `authenticate` / `authenticated` blocks, `authenticate`, `authenticate_user`, `ensure_logged_in`, `login_required`, `require_login`, `require_user`, and `require_logged_in_<user\|moderator\|admin>` (optionally `_or_<status>`). `authorize` is not one: it checks permission, and a policy can admit guests | Wrong is worse than missing |
| `Net::HTTP` / `Faraday` / `HTTParty` call | Outbound | `api_call` | Our code calling out |
| Controller action not named by any route | None | — | No address |
