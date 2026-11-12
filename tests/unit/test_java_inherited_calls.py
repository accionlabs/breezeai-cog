"""A Java call to an inherited method (``foo()``, ``this.foo()``, ``super.foo()``) resolves to
the nearest in-repo ancestor that declares it."""

from __future__ import annotations

from pathlib import Path

from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.java.parser import JavaParser

_ROOT = "src/main/java/com/acme"


def _calls(tmp_path: Path, files: dict[str, str], target: str) -> dict[str, dict[str, str | None]]:
    for rel, src in files.items():
        p = tmp_path / _ROOT / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)
    parser = JavaParser()
    index = parser.build_index(tmp_path, list(tmp_path.rglob("*.java")))
    rel = f"{_ROOT}/{target}"
    path = tmp_path / rel
    ctx = ParseContext(path=rel, abs_path=path, source=path.read_bytes(), repo_root=tmp_path,
                       resolution_index=index)
    record = parser.parse_file(ctx)
    return {f.name: {c.name: c.path for c in f.calls} for f in record.functions}


_BASE = """package com.acme;
public class Base extends Root {
    public void foo() {}
    public void shared() {}
}
"""
_ROOT_CLASS = """package com.acme;
public class Root { public void top() {} public void shared() {} }
"""
_CHILD = """package com.acme.web;
import com.acme.Base;
public class Child extends Base {
    void a() { foo(); }
    void b() { this.foo(); top(); }
    void c() { super.shared(); own(); }
    void own() {}
    void d() { notDeclared(); }
}
"""


def test_inherited_calls_resolve_to_the_declaring_file(tmp_path: Path) -> None:
    calls = _calls(tmp_path, {"Base.java": _BASE, "Root.java": _ROOT_CLASS,
                              "web/Child.java": _CHILD}, "web/Child.java")
    assert calls["a"] == {"foo": f"{_ROOT}/Base.java"}
    assert calls["b"] == {"foo": f"{_ROOT}/Base.java", "top": f"{_ROOT}/Root.java"}
    # nearest ancestor wins (Base overrides Root.shared); a method of its own stays here
    assert calls["c"] == {"shared": f"{_ROOT}/Base.java", "own": f"{_ROOT}/web/Child.java"}
    assert calls["d"] == {"notDeclared": None}


def test_a_base_from_outside_the_repo_ends_the_chain(tmp_path: Path) -> None:
    # `Base` is imported from a library; the repo's own com.acme.Base is a different class.
    child = _CHILD.replace("import com.acme.Base;", "import org.lib.Base;")
    calls = _calls(tmp_path, {"Base.java": _BASE, "Root.java": _ROOT_CLASS,
                              "web/Child.java": child}, "web/Child.java")
    assert calls["a"] == {"foo": None}


def test_a_class_name_declared_twice_is_ambiguous(tmp_path: Path) -> None:
    other = "package com.acme.other;\npublic class Base { public void foo() {} }\n"
    calls = _calls(tmp_path, {"Base.java": _BASE, "Root.java": _ROOT_CLASS,
                              "other/Base.java": other, "web/Child.java": _CHILD},
                   "web/Child.java")
    assert calls["a"] == {"foo": None}


def test_same_package_base_needs_no_import(tmp_path: Path) -> None:
    child = """package com.acme;
public class Child extends Base { void a() { foo(); } }
"""
    calls = _calls(tmp_path, {"Base.java": _BASE, "Root.java": _ROOT_CLASS,
                              "Child.java": child}, "Child.java")
    assert calls["a"] == {"foo": f"{_ROOT}/Base.java"}
