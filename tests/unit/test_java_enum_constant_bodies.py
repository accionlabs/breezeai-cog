"""An enum constant with a body (``RED("r") { … }``) is an anonymous subclass of the enum:
a Class ``Color.RED`` with ``isAnonymous`` and ``extends`` the enum, its methods under it
(java/anonymous.py)."""

from __future__ import annotations

from pathlib import Path

from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.java.parser import JavaParser

SRC = """package com.acme;
public enum Color {
  RED("r") {
    @Override String tag() { return helper(); }
    String helper() { return "x"; }
  },
  GREEN("g"),
  BLUE("b") { String tag() { return new Runnable() { public void run() {} }.toString(); } };
  private final String c;
  Color(String c) { this.c = c; }
  String tag() { return c; }
}
"""


def _record(tmp_path: Path):
    rel = "src/main/java/com/acme/Color.java"
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(SRC)
    ctx = ParseContext(path=rel, abs_path=path, source=SRC.encode(), repo_root=tmp_path,
                       capture_statements=True)
    return JavaParser().parse_file(ctx)


def test_a_constant_body_is_a_class_of_its_own(tmp_path: Path) -> None:
    rec = _record(tmp_path)
    classes = {c.id.rsplit("#", 1)[-1]: c for c in rec.classes}
    assert sorted(classes) == ["Color", "Color.BLUE", "Color.BLUE$1", "Color.RED"]
    red = classes["Color.RED"]
    assert (red.name, red.extends, red.isAnonymous, red.startLine, red.endLine) == (
        "RED", "Color", True, 3, 6)
    assert classes["Color.BLUE$1"].extends is None  # an anonymous class inside the body
    assert classes["Color"].isAnonymous is None


def test_its_methods_are_functions_under_it(tmp_path: Path) -> None:
    rec = _record(tmp_path)
    fns = {f.id.rsplit("#", 2)[-2] + "#" + f.name: f for f in rec.functions}
    assert {"Color.RED#tag", "Color.RED#helper", "Color.BLUE#tag", "Color#tag",
            "Color.BLUE$1#run"} <= set(fns)
    assert [(c.name, c.path) for c in fns["Color.RED#tag"].calls] == [
        ("helper", "src/main/java/com/acme/Color.java")]


def test_the_constants_stay_enum_members(tmp_path: Path) -> None:
    rec = _record(tmp_path)
    members = [s.startLine for s in rec.statements if s.semanticType == "enum_member"]
    assert members == [3, 7, 8]
