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


def _parse(tmp_path, *, capture=False) -> FileRecord:
    p = tmp_path / "main.py"
    p.write_text(SRC.decode())
    ctx = ParseContext(path="main.py", abs_path=p, source=SRC, repo_root=tmp_path,
                       capture_statements=capture, text_truncation_limit=1000)
    return FastAPIParser().parse_file(ctx)


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
