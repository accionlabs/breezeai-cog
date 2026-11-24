"""``new X(…)`` is a call to X's constructor — a call named ``X`` with the file declaring X —
when X resolves, as Java resolves a type name, to another file of the repository
(java/functions.py ``_calls``)."""

from __future__ import annotations

from pathlib import Path

from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.java.parser import JavaParser

_ROOT = "src/main/java"
_A = f"{_ROOT}/a"

FILES = {
    "a/Money.java": "package a;\npublic class Money { long amount; }\n",
    "a/Report.java": "package a;\npublic class Report {\n"
                     "  public Report(String id) {}\n  public Report(String id, Object t) {}\n"
                     "  public void render() {}\n}\n",
    "a/Handler.java": "package a;\npublic class Handler<T> { public Handler(String n) {} }\n",
    "a/Builder.java": """package a;
import java.util.List;
public class Builder {
  Report make() { return new Report("r1"); }
  Money total() { return new Money(); }
  List<Handler<String>> all() { return List.of(new Handler<>("a"), new Handler<String>("b")); }
  Runnable anon() { return new Runnable() { public void run() {} }; }
  Builder self() { return new Builder(); }
  Object lib() { return new StringBuilder(); }
  Object qualified() { return new a.Report("q"); }
}
""",
    "b/Other.java": "package b;\nimport a.Report;\n"
                    "public class Other { Object x() { return new Report(\"r\"); } }\n",
    # a library import hides this package's class of the same name
    "a/Lib.java": "package a;\nimport org.lib.Report;\n"
                  "public class Lib { Object x(Report r) { r.render(); return new Report(\"r\"); } }\n",
}


def _calls(tmp_path: Path, target: str) -> dict[str, dict[str, str | None]]:
    for rel, src in FILES.items():
        p = tmp_path / _ROOT / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)
    parser = JavaParser()
    index = parser.build_index(tmp_path, list(tmp_path.rglob("*.java")))
    rel = f"{_ROOT}/{target}"
    path = tmp_path / rel
    ctx = ParseContext(path=rel, abs_path=path, source=path.read_bytes(), repo_root=tmp_path,
                       resolution_index=index)
    return {f.name: {c.name: c.path for c in f.calls} for f in parser.parse_file(ctx).functions}


def test_a_class_of_another_file_is_a_call_to_its_constructor(tmp_path: Path) -> None:
    calls = _calls(tmp_path, "a/Builder.java")
    assert calls["make"] == {"Report": f"{_A}/Report.java"}  # two constructors: one call
    assert calls["total"] == {"Money": f"{_A}/Money.java"}  # no written constructor: kept
    # generic, diamond, created twice: one call
    assert calls["all"] == {"of": None, "Handler": f"{_A}/Handler.java"}


def test_what_adds_no_call(tmp_path: Path) -> None:
    calls = _calls(tmp_path, "a/Builder.java")
    assert calls["anon"] == {}  # an anonymous class: linked by its `extends` / `implements`
    assert calls["self"] == {}  # this file's own class
    assert calls["lib"] == {}  # a library class
    assert calls["qualified"] == {}  # a qualified name


def test_an_imported_class_resolves(tmp_path: Path) -> None:
    assert _calls(tmp_path, "b/Other.java")["x"] == {"Report": f"{_A}/Report.java"}


def test_a_library_import_hides_the_same_package_class(tmp_path: Path) -> None:
    # `Report` here is org.lib.Report: neither its creation nor a call on it is a/Report.java
    assert _calls(tmp_path, "a/Lib.java")["x"] == {"render": None}
