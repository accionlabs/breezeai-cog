"""FastAPI parser: route detection, parentId linkage, base reuse, override wiring."""

from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from breezeai_cog.core import registry
from breezeai_cog.emit import to_line
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.python_fastapi.parser import FastAPIParser
from breezeai_cog.schemas import FileRecord

SRC = b'''from fastapi import FastAPI, APIRouter

app = FastAPI()
router = APIRouter()


@app.get("/items/{item_id}")
async def read_item(item_id: int):
    return item_id


@router.post("/items")
def create_item(item: dict):
    return item


def helper(x):
    return x
'''


def _parse(tmp_path, *, capture=True) -> FileRecord:
    p = tmp_path / "main.py"
    p.write_text(SRC.decode())
    ctx = ParseContext(path="main.py", abs_path=p, source=SRC, repo_root=tmp_path,
                       capture_statements=capture, statement_text_limit=1000)
    return FastAPIParser().parse_file(ctx)


def test_routes_require_capture_statements(tmp_path) -> None:
    # Routes are statements — only emitted with statement capture (spec A4).
    rec = _parse(tmp_path, capture=False)
    assert [s for s in rec.statements if s.semanticType == "route"] == []
    assert rec.framework is None


def test_routes_detected_and_linked(tmp_path) -> None:
    rec = _parse(tmp_path)
    routes = [s for s in rec.statements if s.semanticType == "route"]
    by_endpoint = {r.endpoint: r for r in routes}
    assert set(by_endpoint) == {"/items/{item_id}", "/items"}

    get = by_endpoint["/items/{item_id}"]
    assert get.method == "GET" and get.framework == "fastapi" and get.handler == "read_item"

    post = by_endpoint["/items"]
    assert post.method == "POST" and post.handler == "create_item"

    # each route is parented to its handler function's id (cross-cut via emit.ids)
    fn_ids = {f.id for f in rec.functions}
    assert all(r.parentId in fn_ids for r in routes)
    assert rec.framework == "fastapi"


def test_base_extraction_reused(tmp_path) -> None:
    rec = _parse(tmp_path)
    # base PythonParser extraction still present (functions, language)
    assert {f.name for f in rec.functions} == {"read_item", "create_item", "helper"}
    assert rec.language == "python"  # framework parser keeps the base language


API_ROUTE_SRC = b'''from fastapi import APIRouter

router = APIRouter()


@router.api_route("/items", methods=["GET", "POST"])
def items():
    return []


@router.api_route("/health")
def health():
    return "ok"
'''


def test_api_route_emits_one_route_per_method(tmp_path) -> None:
    p = tmp_path / "main.py"
    p.write_text(API_ROUTE_SRC.decode())
    ctx = ParseContext(path="main.py", abs_path=p, source=API_ROUTE_SRC, repo_root=tmp_path,
                       capture_statements=True, statement_text_limit=1000)
    rec = FastAPIParser().parse_file(ctx)
    routes = {(r.method, r.endpoint) for r in rec.statements if r.semanticType == "route"}
    # methods=[...] -> one route per verb; bare api_route defaults to GET
    assert routes == {("GET", "/items"), ("POST", "/items"), ("GET", "/health")}


def test_output_validates(tmp_path) -> None:
    rec = _parse(tmp_path, capture=True)
    schema = FileRecord.model_json_schema(by_alias=True)
    errors = list(Draft202012Validator(schema).iter_errors(json.loads(to_line(rec))))
    assert not errors, errors


def test_claims_selects_fastapi() -> None:
    registry.clear()
    from breezeai_cog.parsers.python.parser import PythonParser

    registry.register(PythonParser())
    registry.register(FastAPIParser())
    assert registry.select("x.py", b"from fastapi import FastAPI").name == "python-fastapi"
    assert registry.select("x.py", b"print(1)").name == "python"  # plain python -> base
    registry.clear()


# ── Router prefixes (same file) ───────────────────────────────────────────────────────────────
def _endpoints(analyze_files, src: str) -> dict[str, str | None]:
    st = analyze_files({"main.py": src})
    return {s.handler: s.endpoint for s in st if s.semanticType == "route"}


def test_router_prefix_and_same_file_include_compose(analyze_files) -> None:
    got = _endpoints(analyze_files, (
        "from fastapi import FastAPI, APIRouter\n"
        "app = FastAPI()\n"
        "users = APIRouter(prefix='/users')\n"
        "admin = APIRouter(prefix='/admin')\n"
        "plain = APIRouter()\n"
        "@users.get('/{uid}')\ndef get_user(uid: int):\n    pass\n"
        "@admin.get('/stats')\ndef stats():\n    pass\n"
        "@plain.get('/ping')\ndef ping():\n    pass\n"
        "@app.get('/health')\ndef health():\n    pass\n"
        "admin.include_router(users, prefix='/v1')\n"
        "app.include_router(admin, prefix='/api')\n"
        "app.include_router(plain)\n"
    ))
    assert got == {
        "get_user": "/api/admin/v1/users/{uid}",  # nested include: prefixes concatenate
        "stats": "/api/admin/stats",
        "ping": "/ping",                          # included with no prefix
        "health": "/health",
    }


def test_unresolved_prefix_leaves_endpoint_empty(analyze_files) -> None:
    got = _endpoints(analyze_files, (
        "from fastapi import FastAPI, APIRouter\nfrom app.config import settings\n"
        "app = FastAPI()\n"
        "orders = APIRouter(prefix='/orders')\n"
        "items = APIRouter(prefix=settings.ITEMS)\n"
        "@orders.get('/{oid}')\ndef get_order(oid: int):\n    pass\n"
        "@items.get('/')\ndef list_items():\n    pass\n"
        "app.include_router(orders, prefix=settings.API_V1_PREFIX)\n"
    ))
    # a prefix that exists but has no value: the address is unknown — never a partial path
    assert got == {"get_order": None, "list_items": None}


def test_router_included_twice_or_not_here_keeps_its_own_path(analyze_files) -> None:
    got = _endpoints(analyze_files, (
        "from fastapi import FastAPI, APIRouter\n"
        "app = FastAPI()\n"
        "shared = APIRouter(prefix='/shared')\n"
        "lonely = APIRouter(prefix='/lonely')\n"
        "@shared.get('/x')\ndef x():\n    pass\n"
        "@lonely.get('/y')\ndef y():\n    pass\n"
        "app.include_router(shared, prefix='/v1')\n"
        "app.include_router(shared, prefix='/v2')\n"
    ))
    assert got == {"x": "/shared/x", "y": "/lonely/y"}


def test_empty_path_is_the_router_prefix(analyze_files) -> None:
    got = _endpoints(analyze_files, (
        "from fastapi import APIRouter\nrouter = APIRouter(prefix='/customers')\n"
        "@router.get('')\ndef list_customers():\n    pass\n"
    ))
    assert got == {"list_customers": "/customers"}
