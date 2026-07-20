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
    by_name = {p.name: p for p in top.params}
    assert by_name["b"].default == '"x"'  # default-value expr captured
    assert by_name["a"].default is None  # no default -> None (dropped by exclude_none)
    assert "helper" in [c.name for c in top.calls]
    assert top.id.endswith("@21") or "@" in top.id  # position-suffixed id


def test_param_default_captures_depends(tmp_path) -> None:
    src = (b"from fastapi import Depends\n"
           b"def get_db(): ...\n"
           b"def h(db=Depends(get_db), q: int = 0): return db\n")
    abs_path = tmp_path / "h.py"
    abs_path.write_bytes(src)
    ctx = ParseContext(path="h.py", abs_path=abs_path, source=src, repo_root=tmp_path,
                       capture_statements=True)
    rec = PythonParser().parse_file(ctx)
    h = next(f for f in rec.functions if f.name == "h")
    by_name = {p.name: p for p in h.params}
    assert by_name["db"].default == "Depends(get_db)"
    assert by_name["q"].type == "int" and by_name["q"].default == "0"


def test_nested_defs_extracted(tmp_path) -> None:
    # Regression (code-capture-gap): nested `def`s (closures / decorator factories /
    # in-method helpers) must each be their own Function parented to the enclosing
    # function, and their calls must attribute to them — not fold into the parent.
    # Anonymous lambdas still fold into the nearest named function.
    src = (b"def outer(x):\n"
           b"    def helper(y):\n"
           b"        return compute(y)\n"
           b"    lam = lambda a: sideeffect(a)\n"
           b"    return helper(x)\n"
           b"\n"
           b"class C:\n"
           b"    def method(self):\n"
           b"        def inner():\n"
           b"            return log('hi')\n"
           b"        return inner()\n")
    p = tmp_path / "n.py"
    p.write_bytes(src)
    ctx = ParseContext(path="n.py", abs_path=p, source=src, repo_root=tmp_path,
                       capture_statements=True)
    rec = PythonParser().parse_file(ctx)
    by_name = {f.name: f for f in rec.functions}
    assert {"outer", "helper", "method", "inner"} <= set(by_name)
    assert by_name["helper"].parentId == by_name["outer"].id
    assert by_name["inner"].parentId == by_name["method"].id
    # nested-def call lands on the nested def; the anonymous lambda's call stays on the parent
    assert "compute" in {c.name for c in by_name["helper"].calls}
    assert "compute" not in {c.name for c in by_name["outer"].calls}
    assert "sideeffect" in {c.name for c in by_name["outer"].calls}


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


def test_bare_call_statements_captured(tmp_path) -> None:
    # Regression (limitation B): a bare call-statement (no expression_statement wrapper
    # in this grammar) must be emitted as a `call` statement and classified.
    src = (
        "def process(order, session):\n"
        "    repo.save(order)\n"          # bare db call
        "    mailer.send(order.email)\n"  # bare plain call -> structural statement
    ).encode()
    p = tmp_path / "p.py"
    p.write_text(src.decode())
    ctx = ParseContext(path="p.py", abs_path=p, source=src, repo_root=tmp_path,
                       capture_statements=True)
    rec = PythonParser().parse_file(ctx)
    fn = next(f for f in rec.functions if f.name == "process")
    calls = [s for s in rec.statements if s.nodeType == "call" and s.parentId == fn.id]
    texts = {s.text for s in calls}
    assert any("repo.save(order)" in t for t in texts)      # bare db call now a statement
    assert any("mailer.send" in t for t in texts)           # bare plain call now a statement
    assert any(s.semanticType == "db_method_call" and "repo.save" in s.text for s in calls)


def test_bare_call_in_control_body_not_mislabeled(tmp_path) -> None:
    # The enclosing if/for must not inherit a bare call's semanticType.
    src = "def h(o):\n    if o:\n        repo.save(o)\n".encode()
    p = tmp_path / "q.py"
    p.write_text(src.decode())
    ctx = ParseContext(path="q.py", abs_path=p, source=src, repo_root=tmp_path,
                       capture_statements=True)
    rec = PythonParser().parse_file(ctx)
    ifs = [s for s in rec.statements if s.nodeType == "if_statement"]
    assert ifs and all(s.semanticType is None for s in ifs)
    assert any(s.nodeType == "call" and s.semanticType == "db_method_call" for s in rec.statements)


def test_defs_nested_in_blocks_are_seeded(tmp_path) -> None:
    # Regression: functions/classes nested in module- or class-level block
    # statements (with/if/for/try, e.g. Airflow `with DAG(...):` + `@task def`)
    # must be extracted, not only direct children of the module/class body.
    src = (
        "with DAG('d') as dag:\n"
        "    @task\n"
        "    def extract_task():\n"
        "        repo.load()\n"                 # bare call — must attach to the fn
        "    def _helper():\n"
        "        return 1\n"
        "\n"
        "if FLAG:\n"
        "    for x in items:\n"
        "        def looped():\n"
        "            return x\n"
        "\n"
        "class Svc:\n"
        "    if TYPE_CHECKING:\n"
        "        def guarded(self):\n"
        "            return 2\n"
    ).encode()
    p = tmp_path / "dag.py"
    p.write_text(src.decode())
    ctx = ParseContext(path="dag.py", abs_path=p, source=src, repo_root=tmp_path,
                       capture_statements=True)
    rec = PythonParser().parse_file(ctx)
    names = {f.name for f in rec.functions}
    assert {"extract_task", "_helper", "looped", "guarded"} <= names
    et = next(f for f in rec.functions if f.name == "extract_task")
    assert any(d.name == "task" for d in et.decorators)          # decorator preserved
    guarded = next(f for f in rec.functions if f.name == "guarded")
    assert guarded.type == "method"                              # attached to the class
    # the fn body statement is attributed to the fn, not duplicated at file scope
    load_stmts = [s for s in rec.statements if s.nodeType == "call" and "repo.load" in s.text]
    assert len(load_stmts) == 1 and load_stmts[0].parentId == et.id


def test_endpoint_fstring_and_concat(tmp_path) -> None:
    # #3: f-string / concatenation endpoints resolve to {param} paths (was the raw first arg).
    src = "def f(id):\n    requests.get(f'/users/{id}')\n    requests.get('/a/' + str(id))\n".encode()
    p = tmp_path / "e.py"
    p.write_text(src.decode())
    ctx = ParseContext(path="e.py", abs_path=p, source=src, repo_root=tmp_path, capture_statements=True)
    rec = PythonParser().parse_file(ctx)
    eps = [s.endpoint for s in rec.statements if s.semanticType == "api_call"]
    assert "/users/{id}" in eps
    assert any(e and e.startswith("/a/") for e in eps)
