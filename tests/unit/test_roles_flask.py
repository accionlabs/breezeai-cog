"""Role table: docs/entry-point-roles/flask.md."""

from __future__ import annotations

from breezeai_cog.parsers.contract import is_entry_point

HEAD = "from flask import Flask, Blueprint\n"


def _eps(stmts):
    return [s for s in stmts if is_entry_point(s)]


def _one(stmts, method, endpoint):
    found = [s for s in _eps(stmts) if s.method == method and s.endpoint == endpoint]
    assert len(found) == 1, [(s.method, s.endpoint) for s in _eps(stmts)]
    return found[0]


def test_route_decorator_is_one_binding_per_method(analyze_files) -> None:
    st = analyze_files({"app.py": (
        HEAD + "app = Flask(__name__)\n\n"
        "@app.route('/users/<int:id>', methods=['GET', 'POST'])\n"
        "def user(id):\n    return ''\n"
        "@app.route('/')\ndef index():\n    return ''\n"
    )})
    r = _one(st, "POST", "/users/<int:id>")
    assert (r.routeKind, r.framework, r.handler, r.handlerLine) == ("route", "flask", "user", 5)
    _one(st, "GET", "/users/<int:id>")
    assert _one(st, "GET", "/").handler == "index"  # no methods= → GET


def test_verb_decorator_is_binding(analyze_files) -> None:
    st = analyze_files({"app.py": (
        HEAD + "app = Flask(__name__)\n"
        "@app.delete('/items/<id>')\ndef remove(id):\n    return ''\n"
    )})
    assert _one(st, "DELETE", "/items/<id>").handler == "remove"


def test_add_url_rule_with_function_is_binding(analyze_files) -> None:
    st = analyze_files({"app.py": (
        HEAD + "app = Flask(__name__)\n"
        "def ping():\n    return ''\n"
        "app.add_url_rule('/ping', 'ping', ping, methods=['HEAD', 'GET'])\n"
    )})
    assert {(s.method, s.handler, s.handlerLine) for s in _eps(st)} == {
        ("HEAD", "ping", 3), ("GET", "ping", 3)}


def test_method_view_is_one_binding_per_method(analyze_files) -> None:
    st = analyze_files({"app.py": (
        HEAD + "from flask.views import MethodView\napp = Flask(__name__)\n"
        "class UserAPI(MethodView):\n"
        "    def get(self, id):\n        return ''\n"
        "    def delete(self, id):\n        return ''\n"
        "app.add_url_rule('/users/<int:id>', view_func=UserAPI.as_view('user'))\n"
    )})
    assert _one(st, "GET", "/users/<int:id>").handlerLine == 5
    assert _one(st, "DELETE", "/users/<int:id>").handler == "delete"


def test_method_view_from_another_file(analyze_files) -> None:
    st = analyze_files({
        "app/views.py": (
            "from flask.views import MethodView\n"
            "class UserAPI(MethodView):\n    def get(self):\n        return ''\n"
        ),
        "app/main.py": (
            HEAD + "from app.views import UserAPI\napp = Flask(__name__)\n"
            "app.add_url_rule('/users', view_func=UserAPI.as_view('users'))\n"
        ),
    })
    r = _one(st, "GET", "/users")
    assert (r.handler, r.handlerLine, r.handlerPath) == ("get", 3, "app/views.py")


def test_view_with_dispatch_request(analyze_files) -> None:
    st = analyze_files({"app.py": (
        HEAD + "from flask.views import View\napp = Flask(__name__)\n"
        "class Report(View):\n    methods = ['GET', 'POST']\n"
        "    def dispatch_request(self):\n        return ''\n"
        "app.add_url_rule('/report', view_func=Report.as_view('report'))\n"
    )})
    assert sorted((s.method, s.handler) for s in _eps(st)) == [
        ("GET", "dispatch_request"), ("POST", "dispatch_request")]


def test_methods_not_a_literal_gives_one_record_without_method(analyze_files) -> None:
    st = analyze_files({"app.py": (
        HEAD + "app = Flask(__name__)\nVERBS = ['GET']\n"
        "@app.route('/x', methods=VERBS)\ndef x():\n    return ''\n"
    )})
    (r,) = _eps(st)
    assert (r.method, r.endpoint) == (None, "/x")


def test_blueprint_prefix_is_wiring(analyze_files) -> None:
    st = analyze_files({"app.py": (
        HEAD + "app = Flask(__name__)\nbp = Blueprint('auth', __name__, url_prefix='/auth')\n"
        "@bp.route('/login')\ndef login():\n    return ''\n"
        "app.register_blueprint(bp)\n"
    )})
    assert [s.endpoint for s in _eps(st)] == ["/auth/login"]


def test_register_blueprint_across_files_and_its_prefix_wins(analyze_files) -> None:
    st = analyze_files({
        "app/__init__.py": (
            HEAD + "def create_app():\n    app = Flask(__name__)\n"
            "    from app.auth import bp as auth_bp\n"
            "    app.register_blueprint(auth_bp, url_prefix='/v1/auth')\n    return app\n"
        ),
        "app/auth/__init__.py": "from flask import Blueprint\nbp = Blueprint('auth', __name__, url_prefix='/auth')\n",
        "app/auth/routes.py": (
            "from app.auth import bp\n"
            "@bp.route('/login', methods=['POST'])\ndef login():\n    return ''\n"
        ),
    })
    assert [s.path for s in _eps(st)] == ["app/auth/routes.py"]  # the registration is no entry point
    _one(st, "POST", "/v1/auth/login")


def test_nested_blueprints_join_prefixes(analyze_files) -> None:
    st = analyze_files({"app.py": (
        HEAD + "app = Flask(__name__)\nparent = Blueprint('p', __name__, url_prefix='/api')\n"
        "child = Blueprint('c', __name__, url_prefix='/users')\n"
        "@child.get('/<id>')\ndef get_user(id):\n    return ''\n"
        "parent.register_blueprint(child)\napp.register_blueprint(parent)\n"
    )})
    _one(st, "GET", "/api/users/<id>")


def test_blueprint_registered_twice_or_nowhere_has_no_address(analyze_files) -> None:
    st = analyze_files({"app.py": (
        HEAD + "app = Flask(__name__)\n"
        "twice = Blueprint('t', __name__)\nnowhere = Blueprint('n', __name__, url_prefix='/n')\n"
        "@twice.get('/a')\ndef a():\n    return ''\n"
        "@nowhere.get('/b')\ndef b():\n    return ''\n"
        "app.register_blueprint(twice, url_prefix='/v1')\napp.register_blueprint(twice, url_prefix='/v2')\n"
    )})
    assert sorted((s.handler, s.endpoint) for s in _eps(st)) == [("a", None), ("b", None)]


def test_prefix_not_a_literal_has_no_address(analyze_files) -> None:
    st = analyze_files({"app.py": (
        HEAD + "app = Flask(__name__)\nbp = Blueprint('b', __name__)\n"
        "@bp.get('/x')\ndef x():\n    return ''\n"
        "app.register_blueprint(bp, url_prefix=settings.PREFIX)\n"
    )})
    assert [s.endpoint for s in _eps(st)] == [None]


def test_app_in_create_app_is_the_root(analyze_files) -> None:
    st = analyze_files({"app.py": (
        HEAD + "def create_app():\n    app = Flask(__name__)\n"
        "    @app.route('/health')\n    def health():\n        return ''\n    return app\n"
    )})
    r = _one(st, "GET", "/health")
    assert r.parentId == "app.py#health@5"


def test_unknown_receiver_has_no_address(analyze_files) -> None:
    st = analyze_files({"wsgi.py": (
        "from flask import current_app\nfrom factory import create_app\napp = create_app()\n"
        "@app.route('/x')\ndef x():\n    return ''\n"
    )})
    (r,) = _eps(st)
    assert (r.method, r.endpoint, r.handler) == ("GET", None, "x")


def test_decorators_below_the_route_are_guards(analyze_files) -> None:
    st = analyze_files({"app.py": (
        HEAD + "from flask_login import login_required\napp = Flask(__name__)\n"
        "@app.route('/me')\n@login_required\n@roles_required('admin')\ndef me():\n    return ''\n"
    )})
    r = _one(st, "GET", "/me")
    assert (r.guards, r.authRequired) == (["login_required", "roles_required"], True)


def test_own_login_required_makes_no_auth_claim(analyze_files) -> None:
    st = analyze_files({"app.py": (
        HEAD + "from app.auth import login_required\napp = Flask(__name__)\n"
        "@app.route('/me')\n@login_required\ndef me():\n    return ''\n"
    )})
    r = _one(st, "GET", "/me")
    assert (r.guards, r.authRequired) == (["login_required"], None)


def test_decorators_above_the_route_are_not_guards(analyze_files) -> None:
    st = analyze_files({"app.py": (
        HEAD + "app = Flask(__name__)\n"
        "@cached\n@app.route('/x')\ndef x():\n    return ''\n"
    )})
    assert _one(st, "GET", "/x").guards is None


def test_before_request_hooks_are_guards(analyze_files) -> None:
    st = analyze_files({"app.py": (
        HEAD + "app = Flask(__name__)\nbp = Blueprint('b', __name__, url_prefix='/b')\n"
        "other = Blueprint('o', __name__, url_prefix='/o')\n"
        "@app.before_request\ndef everywhere():\n    pass\n"
        "@bp.before_request\ndef only_bp():\n    pass\n"
        "@other.before_app_request\ndef app_wide():\n    pass\n"
        "@bp.get('/x')\ndef x():\n    return ''\n"
        "@other.get('/y')\ndef y():\n    return ''\n"
        "app.register_blueprint(bp)\napp.register_blueprint(other)\n"
    )})
    assert _one(st, "GET", "/b/x").guards == ["app_wide", "everywhere", "only_bp"]
    assert _one(st, "GET", "/o/y").guards == ["app_wide", "everywhere"]


def test_startup_and_error_handlers_are_none(analyze_files) -> None:
    st = analyze_files({"app.py": (
        HEAD + "app = Flask(__name__)\n"
        "@app.errorhandler(404)\ndef missing(e):\n    return ''\n"
        "@app.teardown_request\ndef done(exc):\n    pass\n"
        "if __name__ == '__main__':\n    app.run()\n"
    )})
    assert _eps(st) == []


def test_http_client_call_is_outbound(analyze_files) -> None:
    st = analyze_files({"app.py": (
        HEAD + "import requests\napp = Flask(__name__)\n"
        "@app.get('/proxy')\ndef proxy():\n    return requests.get('https://example.com/x').text\n"
    )})
    assert [s.method for s in st if s.semanticType == "api_call"] == ["GET"]
    assert len(_eps(st)) == 1


# ── Flask-RESTful, Flask-RESTX, Flask-Smorest ────────────────────────────────────────────

TODO = ("class Todo(Resource):\n"
        "    def get(self, todo_id):\n        pass\n"
        "    def put(self, todo_id):\n        pass\n")


def test_restful_add_resource_is_one_binding_per_url_and_method(analyze_files) -> None:
    st = analyze_files({
        "app.py": ("from flask import Flask\nfrom flask_restful import Api\nfrom resources import Todo\n"
                   "app = Flask(__name__)\napi = Api(app, prefix='/v1')\n"
                   "api.add_resource(Todo, '/todos/<todo_id>', '/todo/<todo_id>')\n"),
        "resources.py": "from flask_restful import Resource\n" + TODO,
    })
    assert sorted((s.method, s.endpoint, s.handler, s.handlerLine, s.handlerPath) for s in _eps(st)) == [
        ("GET", "/v1/todo/<todo_id>", "get", 3, "resources.py"),
        ("GET", "/v1/todos/<todo_id>", "get", 3, "resources.py"),
        ("PUT", "/v1/todo/<todo_id>", "put", 5, "resources.py"),
        ("PUT", "/v1/todos/<todo_id>", "put", 5, "resources.py")]
    r = _one(st, "GET", "/v1/todo/<todo_id>")
    assert (r.framework, r.routeKind, r.path) == ("flask", "route", "app.py")


def test_restful_api_on_a_blueprint_and_init_app(analyze_files) -> None:
    st = analyze_files({
        "api.py": ("from flask import Blueprint\nfrom flask_restful import Api, Resource\n"
                   "bp = Blueprint('api', __name__, url_prefix='/api')\napi = Api(bp)\n" + TODO
                   + "api.add_resource(Todo, '/todos/<todo_id>')\n"),
        "late.py": ("from flask_restful import Api, Resource\napi = Api()\n" + TODO
                    + "api.add_resource(Todo, '/late')\n"),
        "app.py": ("from flask import Flask\nfrom api import bp\nfrom late import api as late_api\n"
                   "def create_app():\n    app = Flask(__name__)\n    app.register_blueprint(bp)\n"
                   "    late_api.init_app(app)\n    return app\n"),
    })
    assert sorted(((s.method, s.endpoint) for s in _eps(st)), key=str) == [
        ("GET", "/api/todos/<todo_id>"), ("GET", "/late"), ("PUT", "/api/todos/<todo_id>"), ("PUT", "/late")]


def test_restx_namespace_route_on_a_class(analyze_files) -> None:
    st = analyze_files({
        "zoo/__init__.py": ("from flask_restx import Api\nfrom .cat import api as cat_api\n"
                            "api = Api(prefix='/api')\napi.add_namespace(cat_api)\n"),
        "zoo/cat.py": ("from flask_restx import Namespace, Resource\napi = Namespace('cats')\n"
                       "@api.route('/<id>')\n@api.param('id', 'The cat')\nclass Cat(Resource):\n"
                       "    def get(self, id):\n        pass\n"),
        "app.py": "from flask import Flask\nfrom zoo import api\napp = Flask(__name__)\napi.init_app(app)\n",
    })
    (r,) = _eps(st)
    assert (r.method, r.endpoint, r.handler, r.handlerLine, r.path) == ("GET", "/api/cats/<id>", "get", 6, "zoo/cat.py")


def test_restx_api_namespace_with_a_path_and_add_namespace_path(analyze_files) -> None:
    st = analyze_files({"app.py": (
        "from flask import Flask\nfrom flask_restx import Api, Namespace, Resource\n"
        "app = Flask(__name__)\napi = Api(app)\n"
        "todos = api.namespace('todos', path='/tasks/')\n"
        "dogs = Namespace('dogs')\napi.add_namespace(dogs, path='/hounds')\n"
        "@todos.route('/')\nclass List(Resource):\n    def post(self):\n        pass\n"
        "@dogs.route('/<id>')\nclass Dog(Resource):\n    def delete(self, id):\n        pass\n"
        "@api.route('/ping')\nclass Ping(Resource):\n    def get(self):\n        pass\n")})
    assert sorted((s.method, s.endpoint) for s in _eps(st)) == [
        ("DELETE", "/hounds/<id>"), ("GET", "/ping"), ("POST", "/tasks/")]


def test_smorest_blueprint_route_on_a_method_view(analyze_files) -> None:
    st = analyze_files({
        "resources/store.py": ("from flask.views import MethodView\nfrom flask_smorest import Blueprint\n"
                               "blp = Blueprint('stores', __name__, url_prefix='/shop')\n"
                               "@blp.route('/store/<id>')\nclass Store(MethodView):\n"
                               "    def get(self, id):\n        pass\n    def delete(self, id):\n        pass\n"
                               "@blp.route('/health')\ndef health():\n    pass\n"),
        "app.py": ("from flask import Flask\nfrom flask_smorest import Api\n"
                   "from resources.store import blp as StoreBlueprint\n"
                   "def create_app():\n    app = Flask(__name__)\n    api = Api(app)\n"
                   "    api.register_blueprint(StoreBlueprint)\n    return app\n"),
    })
    assert sorted((s.method, s.endpoint, s.handler) for s in _eps(st)) == [
        ("DELETE", "/shop/store/<id>", "delete"), ("GET", "/shop/health", "health"),
        ("GET", "/shop/store/<id>", "get")]


def test_rest_resources_without_a_known_container(analyze_files) -> None:
    st = analyze_files({"app.py": (
        "from flask_restful import Api\nfrom ext import api, Remote\n" + "from flask_restful import Resource\n"
        + TODO + "api.add_resource(Todo, '/todos')\napi.add_resource(Remote, '/remote')\n")})
    assert sorted(((s.method, s.endpoint, s.handler) for s in _eps(st)), key=str) == [
        ("GET", None, "get"), ("PUT", None, "put"), (None, None, "Remote")]


def test_add_resource_and_class_route_of_something_else_are_not_routes(analyze_files) -> None:
    st = analyze_files({"app.py": (
        "from flask import Flask\napp = Flask(__name__)\nregistry.add_resource(Thing, '/x')\n"
        "@router.route('/y')\nclass Y:\n    def get(self):\n        pass\n")})
    assert _eps(st) == []


def test_smorest_blueprint_registered_where_the_code_does_not_show_has_no_address(analyze_files) -> None:
    st = analyze_files({"app.py": (
        "from flask import Flask\nfrom flask.views import MethodView\nfrom flask_smorest import Api, Blueprint\n"
        "app = Flask(__name__)\nblp = Blueprint('b', __name__)\nApi(app).register_blueprint(blp)\n"
        "@blp.route('/m')\nclass M(MethodView):\n    def post(self):\n        pass\n")})
    assert [(s.method, s.endpoint, s.handler) for s in _eps(st)] == [("POST", None, "post")]
