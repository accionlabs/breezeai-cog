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


_PARAM_BASE = """package com.acme;
public class BaseParam { public String getActor() { return ""; } }
"""
_PARAM = """package com.acme;
import org.json.JsonObject;
public class OrderParam extends BaseParam {
    public String getOrder() { return ""; }
}
"""
_RESPONSE = """package com.acme;
public class Response { public String body() { return ""; } }
"""
_USER = """package com.acme.web;
import com.acme.OrderParam;
import com.acme.Response;
import org.json.JsonObject;
public class User {
    void a(OrderParam p) { p.getOrder(); p.getActor(); p.getMissing(); }
    void b(Response response) { response.body(); }
    void c() { JsonObject response = new JsonObject(); response.putString("k", "v"); }
}
"""


def test_a_typed_receiver_resolves_to_the_file_that_declares_the_method(tmp_path: Path) -> None:
    calls = _calls(tmp_path, {"BaseParam.java": _PARAM_BASE, "OrderParam.java": _PARAM,
                              "Response.java": _RESPONSE, "web/User.java": _USER},
                   "web/User.java")
    # declared on the type → its file; inherited → the ancestor's file; declared nowhere → null
    assert calls["a"] == {"getOrder": f"{_ROOT}/OrderParam.java",
                          "getActor": f"{_ROOT}/BaseParam.java", "getMissing": None}
    assert calls["b"] == {"body": f"{_ROOT}/Response.java"}
    # `response` is a Response in b() but a library JsonObject here: Response has no putString
    assert calls["c"] == {"putString": None}


_HANDLER_BASE = """package com.acme;
public class ApiHandler { public void send(String url) {} }
"""
_DATA_HANDLER = """package com.acme;
public class DataApiHandler extends ApiHandler {
    public void get(String table) {}
}
"""
_BUILDER = """package com.acme;
import java.util.List;
public class Builder {
    public DataApiHandler dataApi() { return new DataApiHandler(); }
    public static Builder create() { return new Builder(); }
    public List<String> names() { return null; }
    public void done() {}
}
"""
_SERVICE = """package com.acme.web;
import com.acme.Builder;
public class Service extends Base2 {
    void a(Builder builder) { builder.dataApi().get("T"); }
    void b(Builder builder) { builder.dataApi().send("/x"); }
    void c() { Builder.create().done(); }
    void d(Builder builder) { builder.names().get(0); }
    void e(Builder builder) { builder.dataApi().missing(); }
    void f() { own().get("T"); }
    void g(Builder builder) { builder.create().dataApi().get("T"); }
    com.acme.DataApiHandler own() { return null; }
}
"""
_BASE2 = """package com.acme.web;
public class Base2 {}
"""


def test_a_call_on_a_call_resolves_through_the_return_type(tmp_path: Path) -> None:
    calls = _calls(tmp_path, {"ApiHandler.java": _HANDLER_BASE,
                              "DataApiHandler.java": _DATA_HANDLER, "Builder.java": _BUILDER,
                              "web/Service.java": _SERVICE, "web/Base2.java": _BASE2},
                   "web/Service.java")
    assert calls["a"]["get"] == f"{_ROOT}/DataApiHandler.java"
    assert calls["b"]["send"] == f"{_ROOT}/ApiHandler.java"     # inherited by the return type
    assert calls["c"]["done"] == f"{_ROOT}/Builder.java"        # static factory on a class
    assert calls["d"]["get"] is None                            # a library return type
    assert calls["e"]["missing"] is None                        # not declared on the type
    assert calls["f"]["get"] == f"{_ROOT}/DataApiHandler.java"  # own method's return type
    assert calls["g"]["get"] is None                            # two calls deep: not followed


_NOTICE = """package com.acme;
public class Notice {
    protected void alert(String m) {}
    protected void log(String m) {}
}
"""
_DATA = """package com.acme;
public class Data extends Notice {
    private final Handler h = new Handler() {
        @Override protected void alert(String m) {}
    };
    void a() { alert("x"); }
    void b() { this.alert("x"); }
    void c() { own(); }
    void own() {}
    void d() {
        new Handler() {
            void run() { alert("y"); this.log("z"); }
        };
    }
    class Inner {
        void e() { own(); alert("q"); }
    }
}
"""
_HANDLER = """package com.acme;
public abstract class Handler {
    protected void alert(String m) {}
}
"""


def test_a_bare_call_is_not_shadowed_by_another_class_in_the_file(tmp_path: Path) -> None:
    calls = _calls(tmp_path, {"Notice.java": _NOTICE, "Data.java": _DATA,
                              "Handler.java": _HANDLER}, "Data.java")
    # `alert` is declared here only by an anonymous Handler: Data's own call is the inherited one
    assert calls["a"] == {"alert": f"{_ROOT}/Notice.java"}
    assert calls["b"] == {"alert": f"{_ROOT}/Notice.java"}
    assert calls["c"] == {"own": f"{_ROOT}/Data.java"}
    # an inner class reaches its outer class's methods; its own unknown name goes up Inner's chain
    assert calls["e"] == {"own": f"{_ROOT}/Data.java", "alert": None}


def test_a_call_inside_an_anonymous_class_keeps_the_file_wide_answer(tmp_path: Path) -> None:
    calls = _calls(tmp_path, {"Notice.java": _NOTICE, "Data.java": _DATA,
                              "Handler.java": _HANDLER}, "Data.java")
    # in the anonymous Handler, `alert` may be its own: it stays credited to this file as before,
    # and `this.log` (the anonymous instance) is not resolved through Data's base class
    assert calls["d"] == {"alert": f"{_ROOT}/Data.java", "log": None}
