"""Role table: docs/entry-point-roles/fastapi.md."""

from __future__ import annotations

import pytest

EP = {"route", "eventbus_consumer", "timer"}


def _eps(stmts):
    return [s for s in stmts if s.semanticType in EP and s.routeKind != "field_resolver"]


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


@pytest.mark.xfail(
    strict=True, reason="include_router(prefix=...) in another file not joined into the endpoint"
)
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


@pytest.mark.xfail(strict=True, reason="Depends(...) dependencies not recorded in guards")
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


@pytest.mark.xfail(strict=True, reason="@app.middleware functions not recorded in guards")
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
