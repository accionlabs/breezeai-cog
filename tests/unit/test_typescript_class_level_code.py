"""Code of a class outside its methods and function fields (anonymous-functions Rule 3, JS/TS):
field initializers and static blocks. A named function in it is a Function of the class; the
rest — nameless callbacks included — is the class's own: its statements and ``Class.calls``."""

from __future__ import annotations

from pathlib import Path

from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.typescript.parser import TypeScriptParser


def _parse(tmp_path: Path, src: str):
    p = tmp_path / "app.ts"
    p.write_text(src)
    ctx = ParseContext(path="app.ts", abs_path=p, source=p.read_bytes(), repo_root=tmp_path,
                       capture_statements=True)
    return TypeScriptParser().parse_file(ctx)


SRC = """class Svc {
  matchers = { name: (a, b) => a.toLowerCase() };
  handle = asyncHandler(async () => load());
  static { register(() => boot()); }
  @Input() size = compute();
  run() { go(); }
  click = () => fire();
  inner = class { x = deep(); };
}
"""


def test_class_level_code_gives_class_calls(tmp_path: Path) -> None:
    [cls] = [c for c in _parse(tmp_path, SRC).classes if c.name == "Svc"]
    # not the decorator, the methods, the function fields or the nested class
    assert [(c.name, c.path) for c in cls.calls] == [
        ("asyncHandler", None), ("load", None), ("register", None), ("boot", None),
        ("compute", None)]


def test_named_functions_in_class_level_code_are_the_classes(tmp_path: Path) -> None:
    rec = _parse(tmp_path, SRC)
    fns = {f.name: (f.parentId, [c.name for c in f.calls]) for f in rec.functions}
    assert fns["name"] == ("app.ts#Svc", ["toLowerCase"])
    assert fns["run"][1] == ["go"] and fns["click"][1] == ["fire"]


def test_statements_in_a_static_block_callback_are_the_classes(tmp_path: Path) -> None:
    rec = _parse(tmp_path, """class Boot {
  static {
    on('ready', () => {
      start();
    });
  }
}
""")
    own = [s.text for s in rec.statements if s.parentId == "app.ts#Boot"]
    assert "start();" in own
    assert [c.name for c in rec.classes[0].calls] == ["on", "start"]


def test_a_class_without_own_code_has_no_calls(tmp_path: Path) -> None:
    rec = _parse(tmp_path, "class Plain { a = 1; m() { x(); } }\n")
    assert rec.classes[0].calls is None
