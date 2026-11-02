# Flask — entry-point roles

Parser: `src/breezeai_cog/parsers/python_flask/routes.py` (additive detector — a blueprint's routes often sit in a file that never imports Flask, so no parser could claim it), `python_flask/wiring.py` (blueprint wiring across files, a registered fact) · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_flask.py`

A route's address is its rule joined to the blueprint prefixes it is served under, the way Flask
joins them (`/api` + `/users` → `/api/users`). Converters are kept as written (`/users/<int:id>`).

| Construct | Role | Record | Why |
|---|---|---|---|
| `@app.route(rule)` / `@bp.route(rule, methods=[…])` on a function | Binding, one per method | `route`, verb (`GET` when `methods` is absent), `routeKind: route`, full path, `handler` = function | One handler, inbound HTTP |
| `@app.get(rule)` / `.post` / `.put` / `.patch` / `.delete` (Flask 2.0) | Binding | as above, the verb of the decorator | One handler |
| `app.add_url_rule(rule, endpoint, view_func)` with a function | Binding, one per method | as above; `handler` = the function named by `view_func` | One handler |
| `app.add_url_rule(rule, view_func=UserAPI.as_view("users"))` — a `MethodView` | Binding, one per HTTP method the class defines | `handler` = the method (`get`, `post`, …) at its line; a class whose methods are not visible (inherited) gives one record with no `method`, `handler` = the class | One handler per method |
| `View` subclass with `dispatch_request` and `methods = […]` | Binding, one per method in `methods` (`GET` when absent) | `handler` = `dispatch_request` | One handler |
| `methods=` that is not a literal list | Binding | one record, no `method` | The verbs can't be read |
| `bp = Blueprint(name, __name__, url_prefix=…)` | Wiring | statement stays plain; `url_prefix` joined into the blueprint's routes | Applies to many handlers |
| `app.register_blueprint(bp, url_prefix=…)` — also from another file, and a blueprint registered on a blueprint | Wiring | statement stays plain; the registration's `url_prefix` replaces the blueprint's own, and nested blueprints join their prefixes. A blueprint registered nowhere the code shows (a loop, a variable), or at two places, or with a prefix that is not a literal → its routes' `endpoint` is empty | Points at a blueprint |
| `app = Flask(__name__)` — at module level or inside `create_app()` | Wiring | — (the app is the root: its own routes are served from `/`) | The application |
| A route on a receiver the code does not show to be an app or a blueprint (`app = create_app()`, `current_app`) | Binding | as above, `endpoint` empty | The address can't be resolved |
| Decorators **below** the route decorator (`@login_required`, `@roles_required("admin")`) | Guard | names in `guards`, outermost first. `authRequired: true` only for Flask-Login's `login_required` / `fresh_login_required` imported from `flask_login` | They wrap the registered view and run before it |
| Decorators **above** the route decorator | None | — | Applied after the view is registered: they don't wrap the route |
| `@app.before_request` / `@bp.before_request` / `bp.before_app_request` | Guard | function name in `guards` of every route of the app (`before_app_request` too) or of the blueprint and the blueprints registered on it | Run before many handlers |
| `app.run()`, `@app.errorhandler`, `@app.teardown_request` | None | — | Start-up and error handling |
| `requests` / `httpx` call | Outbound | `api_call` | Our code calling out |

Not recorded yet: Flask-RESTful / Flask-RESTX resources (`api.add_resource(Resource, "/x")`) and
class routes of extension blueprints (Flask-Smorest `@blp.route` on a `MethodView`).
