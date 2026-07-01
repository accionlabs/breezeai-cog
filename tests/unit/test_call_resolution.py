"""calls[].path resolution (spec C4.2/C6, drives CALLS) — Tiers 1 (import) + 2 (same file)."""

from __future__ import annotations

from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.callresolve import make_resolver
from breezeai_cog.parsers.java.parser import JavaParser
from breezeai_cog.parsers.python.parser import PythonParser
from breezeai_cog.parsers.typescript.parser import TypeScriptParser


def test_resolver_tiers() -> None:
    r = make_resolver(bindings={"find_by_id": "repo.py", "Repo": "repo.py"},
                      local_defs={"helper", "get"}, path="svc.py",
                      types={"repo": "Repo", "buf": "StringBuilder"})
    assert r("find_by_id", None) == "repo.py"      # Tier 1: imported function
    assert r("save", "Repo") == "repo.py"          # Tier 1: Imported.method → Imported's file
    assert r("helper", None) == "svc.py"           # Tier 2: same-file function
    assert r("get", "self") == "svc.py"            # Tier 2: own method (self/this)
    assert r("save", "repo") == "repo.py"          # Phase 2: repo:Repo → Repo's file
    assert r("save", "this.repo") == "repo.py"     # Phase 2: this.repo:Repo → Repo's file
    assert r("append", "buf") is None              # type StringBuilder not in bindings → null
    assert r("unknown", None) is None              # unresolved → null
    assert r("m", "someLocalObj") is None          # unknown, untyped receiver → null


def _calls_of(rec, fn_name):
    fn = next(f for f in rec.functions if f.name == fn_name)
    return {c.name: c.path for c in fn.calls}


def test_python_cross_file(tmp_path) -> None:
    (tmp_path / "repo.py").write_text("def find_by_id(i):\n    return i\n")
    p = tmp_path / "svc.py"
    p.write_text("from repo import find_by_id\n\n"
                 "def get(i):\n    return find_by_id(i) + helper()\n\n"
                 "def helper():\n    return external.call()\n")
    rec = PythonParser().parse_file(ParseContext(path="svc.py", abs_path=p, source=p.read_bytes(), repo_root=tmp_path))
    calls = _calls_of(rec, "get")
    assert calls["find_by_id"] == "repo.py"   # imported
    assert calls["helper"] == "svc.py"        # same-file
    assert _calls_of(rec, "helper")["call"] is None  # external → null


def test_typescript_cross_file(tmp_path) -> None:
    (tmp_path / "util.ts").write_text("export function fmt(x: string) { return x; }\n")
    p = tmp_path / "svc.ts"
    p.write_text("import { fmt } from './util';\n"
                 "export function run(x: string) { return fmt(x) + local(); }\n"
                 "function local() { return axios.get('/x'); }\n")
    rec = TypeScriptParser().parse_file(ParseContext(path="svc.ts", abs_path=p, source=p.read_bytes(), repo_root=tmp_path))
    calls = _calls_of(rec, "run")
    assert calls["fmt"] == "util.ts"          # imported
    assert calls["local"] == "svc.ts"         # same-file
    assert _calls_of(rec, "local")["get"] is None  # external (axios) → null


def test_java_imported_class(tmp_path) -> None:
    (tmp_path / "Util.java").write_text("package a;\npublic class Util { public static int helper() { return 1; } }\n")
    p = tmp_path / "Svc.java"
    p.write_text("package a;\nimport a.Util;\n"
                 "public class Svc {\n  int run() { return Util.helper(); }\n}\n")
    idx = JavaParser().build_index(tmp_path, list(tmp_path.rglob("*.java")))
    ctx = ParseContext(path="Svc.java", abs_path=p, source=p.read_bytes(), repo_root=tmp_path, resolution_index=idx)
    rec = JavaParser().parse_file(ctx)
    assert _calls_of(rec, "run")["helper"] == "Util.java"  # Tier 1: imported class receiver


def test_java_receiver_type(tmp_path) -> None:
    # Phase 2: `this.repo.findById()` / `repo.findById()` via the field's declared type.
    (tmp_path / "OrderRepo.java").write_text(
        "package a;\npublic class OrderRepo { public Object findById(Long id) { return id; } }\n")
    p = tmp_path / "OrderService.java"
    p.write_text("package a;\nimport a.OrderRepo;\npublic class OrderService {\n"
                 "  private final OrderRepo repo;\n"
                 "  public OrderService(OrderRepo repo) { this.repo = repo; }\n"
                 "  public Object get(Long id) { return this.repo.findById(id); }\n}\n")
    idx = JavaParser().build_index(tmp_path, list(tmp_path.rglob("*.java")))
    ctx = ParseContext(path="OrderService.java", abs_path=p, source=p.read_bytes(), repo_root=tmp_path, resolution_index=idx)
    rec = JavaParser().parse_file(ctx)
    assert _calls_of(rec, "get")["findById"] == "OrderRepo.java"


def test_typescript_receiver_type(tmp_path) -> None:
    # Phase 2: constructor parameter-property `private repo: Repo` → this.repo.find().
    (tmp_path / "repo.ts").write_text("export class Repo { find(id: string) { return id; } }\n")
    p = tmp_path / "svc.ts"
    p.write_text("import { Repo } from './repo';\n"
                 "export class Svc {\n  constructor(private repo: Repo) {}\n"
                 "  run(id: string) { return this.repo.find(id); }\n}\n")
    rec = TypeScriptParser().parse_file(ParseContext(path="svc.ts", abs_path=p, source=p.read_bytes(), repo_root=tmp_path))
    assert _calls_of(rec, "run")["find"] == "repo.ts"
