"""Role table: docs/entry-point-roles/fastapi.md."""

from __future__ import annotations

from breezeai_cog.parsers.contract import is_entry_point


EP = {"route", "eventbus_consumer", "timer"}


def _eps(stmts):
    return [s for s in stmts if is_entry_point(s)]


def _one(stmts, method, endpoint):
    found = [s for s in _eps(stmts) if s.method == method and s.endpoint == endpoint]
    assert len(found) == 1, [(s.method, s.endpoint) for s in _eps(stmts)]
    return found[0]


def test_verb_decorator_is_binding(analyze_files) -> None:
    st = analyze_files(
        {
            "main.py": (
                "from fastapi import FastAPI\napp = FastAPI()\n\n"
                "@app.get('/items/{id}')\nasync def read_item(id: int):\n    return id\n"
            )
        }
    )
    r = _one(st, "GET", "/items/{id}")
    assert (r.routeKind, r.framework, r.handler, r.handlerLine) == (
        "route",
        "fastapi",
        "read_item",
        5,
    )


def test_api_route_is_one_binding_per_verb(analyze_files) -> None:
    st = analyze_files(
        {
            "main.py": (
                "from fastapi import APIRouter\nrouter = APIRouter()\n\n"
                "@router.api_route('/x', methods=['GET', 'PUT'])\ndef multi():\n    pass\n"
            )
        }
    )
    assert sorted(s.method for s in _eps(st)) == ["GET", "PUT"]
    assert {s.handler for s in _eps(st)} == {"multi"}


def test_websocket_is_binding(analyze_files) -> None:
    st = analyze_files(
        {
            "main.py": (
                "from fastapi import FastAPI\napp = FastAPI()\n\n"
                "@app.websocket('/ws')\nasync def ws(sock):\n    pass\n"
            )
        }
    )
    r = _one(st, "WEBSOCKET", "/ws")
    assert (r.routeKind, r.handler) == ("ws", "ws")


def test_router_prefix_is_wiring(analyze_files) -> None:
    st = analyze_files(
        {
            "users.py": (
                "from fastapi import APIRouter\nrouter = APIRouter(prefix='/users')\n\n"
                "@router.post('/')\ndef create_user():\n    pass\n"
            )
        }
    )
    _one(st, "POST", "/users/")


def test_include_router_is_wiring(analyze_files) -> None:
    st = analyze_files(
        {
            "app/main.py": (
                "from fastapi import FastAPI\nfrom app.users import router\napp = FastAPI()\n"
                "app.include_router(router, prefix='/api')\n"
            ),
            "app/users.py": (
                "from fastapi import APIRouter\nrouter = APIRouter()\n\n"
                "@router.get('/users')\ndef list_users():\n    pass\n"
            ),
        }
    )
    assert [s.path for s in _eps(st)] == ["app/users.py"]  # the include itself is no entry point
    _one(st, "GET", "/api/users")


def test_mount_is_wiring(analyze_files) -> None:
    st = analyze_files(
        {
            "main.py": (
                "from fastapi import FastAPI\nfrom fastapi.staticfiles import StaticFiles\napp = FastAPI()\n"
                "app.mount('/static', StaticFiles(directory='s'))\n"
            )
        }
    )
    assert _eps(st) == []


def test_depends_is_guard(analyze_files) -> None:
    st = analyze_files(
        {
            "main.py": (
                "from fastapi import FastAPI, Depends\napp = FastAPI()\n\n"
                "@app.get('/me', dependencies=[Depends(require_user)])\ndef me():\n    pass\n"
            )
        }
    )
    assert _one(st, "GET", "/me").guards == ["require_user"]


def test_middleware_is_guard(analyze_files) -> None:
    st = analyze_files(
        {
            "main.py": (
                "from fastapi import FastAPI\napp = FastAPI()\n\n"
                "@app.middleware('http')\nasync def audit(request, call_next):\n    return await call_next(request)\n\n"
                "@app.get('/a')\ndef a():\n    pass\n"
            )
        }
    )
    assert len(_eps(st)) == 1
    assert _one(st, "GET", "/a").guards == ["audit"]


def test_startup_is_none(analyze_files) -> None:
    st = analyze_files(
        {
            "main.py": (
                "from fastapi import FastAPI\napp = FastAPI()\n\n"
                "@app.on_event('startup')\nasync def boot():\n    pass\n"
            )
        }
    )
    assert _eps(st) == []


def test_http_client_is_outbound(analyze_files) -> None:
    st = analyze_files(
        {
            "client.py": (
                "import httpx\nfrom fastapi import FastAPI\n\n"
                "def fetch():\n    return httpx.get('https://svc.example/api/b')\n"
            )
        }
    )
    assert _eps(st) == []
    assert [s.endpoint for s in st if s.semanticType == "api_call"] == ["https://svc.example/api/b"]


def test_router_from_a_factory_gets_the_include_prefix(analyze_files) -> None:
    # include_router(make_router(), prefix=…): the router the factory returns is served there.
    factory = ("from fastapi import APIRouter\n"
               "def make_router():\n    r = APIRouter()\n"
               "    @r.get('/{uid}')\n    def get_user(uid: int):\n        return {}\n"
               "    return r\n")
    st = analyze_files({
        "app/__init__.py": "",
        "app/main.py": "from fastapi import FastAPI\nfrom app.users import make_router\n"
                       "app = FastAPI()\napp.include_router(make_router(), prefix='/users')\n",
        "app/users.py": factory,
    })
    assert _one(st, "GET", "/users/{uid}").handler == "get_user"


def test_router_from_a_factory_not_found_included_has_no_address(analyze_files) -> None:
    # Where the factory's router is served is unknown: no address, never the bare `/{uid}`.
    st = analyze_files({"app/__init__.py": "", "app/users.py": (
        "from fastapi import APIRouter\ndef make_router():\n    r = APIRouter()\n"
        "    @r.get('/{uid}')\n    def get_user(uid: int):\n        return {}\n    return r\n")})
    assert [(s.method, s.endpoint) for s in _eps(st)] == [("GET", None)]


USERS = ("from fastapi import APIRouter\nrouter = APIRouter(prefix='/users')\n"
         "@router.get('/{uid}')\ndef get_user(uid: int):\n    return {}\n")


def test_app_made_in_a_factory_is_the_root(analyze_files) -> None:
    # create_app() builds the application: its routes and the routers it includes are served
    # from the root, as for a module-level app.
    st = analyze_files({"app/__init__.py": "", "app/users.py": USERS, "app/main.py": (
        "from fastapi import FastAPI\nfrom app.users import router as users_router\n"
        "def create_app():\n    app = FastAPI()\n"
        "    app.include_router(users_router, prefix='/api')\n"
        "    @app.get('/health')\n    def health():\n        return {}\n    return app\n")})
    assert sorted((s.method, s.endpoint) for s in _eps(st)) == [
        ("GET", "/api/users/{uid}"), ("GET", "/health")]


def test_mounted_sub_application_has_no_address(analyze_files) -> None:
    # A sub-application is served under the mount path, which is not followed: no address,
    # never the bare path — whether it is made at module level or by a factory.
    st = analyze_files({"app/__init__.py": "", "app/v1.py": (
        "from fastapi import FastAPI\nv1 = FastAPI()\n"
        "@v1.get('/items')\ndef items():\n    return []\n"), "app/v2.py": (
        "from fastapi import FastAPI\ndef create_v2():\n    v2 = FastAPI()\n"
        "    @v2.get('/orders')\n    def orders():\n        return []\n    return v2\n"),
        "app/main.py": (
        "from fastapi import FastAPI\nfrom app.v1 import v1\nfrom app.v2 import create_v2\n"
        "app = FastAPI()\nsub = create_v2()\napp.mount('/v1', v1)\napp.mount('/v2', sub)\n"
        "@app.get('/ping')\ndef ping():\n    return 1\n")})
    assert sorted((s.method, s.endpoint or "") for s in _eps(st)) == [
        ("GET", ""), ("GET", ""), ("GET", "/ping")]


def test_sub_application_mounted_in_the_same_file_has_no_address(analyze_files) -> None:
    st = analyze_files({"main.py": (
        "from fastapi import FastAPI\napp = FastAPI()\nsub = FastAPI()\n"
        "@sub.get('/items')\ndef items():\n    return []\napp.mount('/v1', sub)\n")})
    assert [(s.method, s.endpoint) for s in _eps(st)] == [("GET", None)]


def test_route_sits_at_its_decorator(analyze_files) -> None:
    # nodeType is the tree-sitter type of the node the record sits at: the decorator.
    st = analyze_files({"main.py": (
        "from fastapi import FastAPI\napp = FastAPI()\n\n"
        "@app.get('/x')\ndef x():\n    pass\n")})
    assert _one(st, "GET", "/x").nodeType == "decorator"


def test_verb_on_a_non_fastapi_object_is_none(analyze_files) -> None:
    # `.get` on a cache in a FastAPI file is not a route — nor is one on a parameter.
    st = analyze_files({"main.py": (
        "from fastapi import FastAPI\nfrom cachelib import Cache\napp = FastAPI()\n"
        "cache = Cache()\n\n"
        "@cache.get('user-profile')\ndef profile():\n    pass\n\n"
        "def register(api):\n    @api.get('/z')\n    def z():\n        pass\n\n"
        "@app.get('/ok')\ndef ok():\n    pass\n")})
    assert [(s.method, s.endpoint) for s in _eps(st)] == [("GET", "/ok")]


_ROUTING = {
    "app/__init__.py": "",
    "app/api/__init__.py": "",
    "app/api/routing.py": "from fastapi import APIRouter\nrouter = APIRouter(prefix='/orders')\n",
    "app/main.py": (
        "from fastapi import FastAPI\nfrom app.api.routing import router\napp = FastAPI()\n"
        "app.include_router(router, prefix='/api')\n"),
}


def test_imported_router_keeps_its_served_prefix(analyze_files) -> None:
    st = analyze_files({**_ROUTING, "app/api/orders.py": (
        "from fastapi import Depends\nfrom app.api.routing import router\n\n"
        "@router.get('/{id}')\ndef get_order(id: str):\n    pass\n")})
    r = _one(st, "GET", "/api/orders/{id}")
    assert (r.path, r.handler, r.nodeType) == ("app/api/orders.py", "get_order", "decorator")


def test_imported_router_in_a_file_without_fastapi(analyze_files) -> None:
    # The decorating file never mentions fastapi: the additive detector reads it.
    st = analyze_files({**_ROUTING, "app/api/orders.py": (
        "from app.api.routing import router as r\nfrom app.api import routing\n\n"
        "@r.get('/{id}')\ndef get_order(id: str):\n    pass\n\n"
        "@routing.router.post('')\ndef create_order():\n    pass\n")})
    assert sorted((s.method, s.endpoint, s.path) for s in _eps(st)) == [
        ("GET", "/api/orders/{id}", "app/api/orders.py"),
        ("POST", "/api/orders", "app/api/orders.py")]
    assert {s.framework for s in _eps(st)} == {"fastapi"}


def test_imported_router_with_runtime_prefix_keeps_the_marker(analyze_files) -> None:
    # A settings prefix is a value read at runtime: the route carries it as a marker, as a router
    # of its own file does.
    st = analyze_files({
        "app/__init__.py": "",
        "app/routing.py": (
            "from fastapi import APIRouter\nfrom app.settings import settings\n"
            "router = APIRouter(prefix=settings.PREFIX)\n"),
        "app/orders.py": (
            "from app.routing import router\n\n@router.get('/x')\ndef x():\n    pass\n")})
    assert [(s.method, s.endpoint) for s in _eps(st)] == [("GET", "${settings.PREFIX}/x")]


def test_imported_router_included_twice_has_no_address(analyze_files) -> None:
    st = analyze_files({
        "app/__init__.py": "",
        "app/routing.py": "from fastapi import APIRouter\nrouter = APIRouter(prefix='/o')\n",
        "app/orders.py": (
            "from app.routing import router\n\n@router.get('/x')\ndef x():\n    pass\n"),
        "app/main.py": (
            "from fastapi import FastAPI\nfrom app.routing import router\nimport app.orders\n"
            "app = FastAPI()\napp.include_router(router, prefix='/a')\n"
            "app.include_router(router, prefix='/b')\n")})
    assert [(s.method, s.endpoint) for s in _eps(st)] == [("GET", None)]


def test_imported_non_router_is_none(analyze_files) -> None:
    # Imported from a repo file, but not a router there — or from a library: no record.
    st = analyze_files({
        "app/__init__.py": "",
        "app/routing.py": "from fastapi import APIRouter\nrouter = APIRouter()\n",
        "app/cache.py": "from cachelib import Cache\ncache = Cache()\n",
        "app/orders.py": (
            "from fastapi import FastAPI\nfrom app.cache import cache\nfrom lib import router\n\n"
            "@cache.get('k')\ndef a():\n    pass\n\n@router.get('/b')\ndef b():\n    pass\n"),
        "app/other.py": (
            "from app.cache import cache\n\n@cache.get('k')\ndef c():\n    pass\n")})
    assert _eps(st) == []
