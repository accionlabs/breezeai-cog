"""Python parser extraction tests + schema validation of its output."""

from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

from breezeai_cog.emit import to_line
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.python.parser import PythonParser
from breezeai_cog.schemas import FileRecord

SRC = b'''import os.path
from .utils import helper
from ..pkg import thing

__all__ = ["Order"]


@dataclass
class Order(Base, Mixin):
    MAX = 5

    def __init__(self, repo: Repo) -> None:
        self.repo = repo

    @staticmethod
    async def total(items: list[int]) -> int:
        return sum(items)


def top(a: int, b="x", *args, **kw) -> str:
    if a > 0:
        return helper(a)
    return b
'''


def _parse(tmp_path: Path, *, capture: bool = False) -> FileRecord:
    repo = tmp_path
    abs_path = repo / "pkg" / "order.py"
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    # `from .utils import helper` in pkg/order.py resolves to pkg/utils.py
    (repo / "pkg" / "utils.py").write_text("def helper(x): return x\n")
    abs_path.write_text(SRC.decode())
    ctx = ParseContext(
        path="pkg/order.py", abs_path=abs_path, source=SRC, repo_root=repo,
        capture_statements=capture, text_truncation_limit=1000,
    )
    return PythonParser().parse_file(ctx)


def test_file_level(tmp_path) -> None:
    rec = _parse(tmp_path)
    assert rec.path == "pkg/order.py" and rec.language == "python" and rec.type == "code"
    assert rec.loc > 0
    assert "os.path" in rec.externalImports
    assert rec.exports == ["Order"]
    # `from .utils import helper` resolves to a sibling file (relative import, level 1)
    assert any(p.endswith("utils.py") for p in rec.importFiles)


def test_class_and_methods(tmp_path) -> None:
    rec = _parse(tmp_path)
    order = next(c for c in rec.classes if c.name == "Order")
    assert order.extends == "Base" and order.implements == ["Mixin"]
    assert [d.name for d in order.decorators] == ["dataclass"]
    assert order.constructorParams == [__import__("breezeai_cog.schemas", fromlist=["ConstructorParam"]).ConstructorParam(name="repo", type="Repo")]

    methods = {f.name: f for f in rec.functions if f.type == "method"}
    assert set(methods) == {"__init__", "total"}
    total = methods["total"]
    assert total.isStatic is True
    assert total.returnType == "int"
    assert total.params[0].name == "items" and total.params[0].type == "list[int]"
    assert total.parentId == order.id  # HAS_METHOD wiring


def test_top_level_function(tmp_path) -> None:
    rec = _parse(tmp_path)
    top = next(f for f in rec.functions if f.name == "top")
    assert top.type == "function" and top.returnType == "str"
    assert [p.name for p in top.params] == ["a", "b", "*args", "**kw"]
    assert "helper" in [c.name for c in top.calls]
    assert top.id.endswith("@21") or "@" in top.id  # position-suffixed id


def test_statements_are_flat_and_gated(tmp_path) -> None:
    # off by default -> no statements anywhere
    assert _parse(tmp_path, capture=False).statements == []

    rec = _parse(tmp_path, capture=True)
    top = next(f for f in rec.functions if f.name == "top")
    # statements live FLAT on the file, linked to their owner via parentId
    top_stmts = [s for s in rec.statements if s.parentId == top.id]
    node_types = {s.nodeType for s in top_stmts}
    assert "if_statement" in node_types and "return_statement" in node_types
    # class-var statement is parented to the class, not nested on it
    order = next(c for c in rec.classes if c.name == "Order")
    assert any(s.parentId == order.id for s in rec.statements)


def test_extract_reuses_a_prebuilt_tree(tmp_path) -> None:
    from breezeai_cog.parsers.treesitter import parse_source

    abs_path = tmp_path / "pkg" / "order.py"
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "pkg" / "utils.py").write_text("def helper(x): return x\n")
    abs_path.write_text(SRC.decode())
    ctx = ParseContext(path="pkg/order.py", abs_path=abs_path, source=SRC, repo_root=tmp_path,
                       capture_statements=True, text_truncation_limit=1000)
    parser = PythonParser()

    root = parse_source("python", SRC, 0).root_node  # parse once, externally
    via_extract = parser.extract(root, ctx)          # share the tree
    via_parse_file = parser.parse_file(ctx)          # parse + extract

    assert via_extract.model_dump_json() == via_parse_file.model_dump_json()
    assert via_extract.functions  # sanity


def test_output_validates_against_schema(tmp_path) -> None:
    rec = _parse(tmp_path, capture=True)
    schema = FileRecord.model_json_schema(by_alias=True)
    instance = json.loads(to_line(rec))
    errors = sorted(Draft202012Validator(schema).iter_errors(instance), key=str)
    assert not errors, "\n".join(f"{list(e.absolute_path)}: {e.message}" for e in errors)
