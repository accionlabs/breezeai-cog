"""Java ``calls[].path`` for the shapes legacy Vert.x 2 services are full of: a name called on
both a library object and a repository class, ``Outer.this.m()`` inside an anonymous handler,
and a call on a field an in-repo base class declares. Each rule has its unresolved twin: a
library call, an unknown type or an unseen field stays ``None``."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.java.parser import JavaParser, fieldless_types

_ROOT = "src/main/java/com/acme"

_HELPER = """package com.acme;
public class Helper { public static void util() {} public void inst() {} }
"""
_JSON_UTIL = """package com.acme;
import org.vertx.java.core.json.JsonObject;
public class JsonUtil { public static String getString(JsonObject o, String k) { return null; } }
"""
_BASE = """package com.acme;
import org.lib.Client;
public abstract class Base {
    protected Helper helper;
    Helper packageHelper;
    private Helper hidden;
    protected Client client;
    protected Helper shadowed;
}
"""


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
    # a method of an anonymous class is keyed by its class: `Svc$1.handle`
    return {(f"{f.parentId.rsplit('#', 1)[-1]}.{f.name}" if "$" in f.parentId else f.name):
            {c.name: c.path for c in f.calls} for f in record.functions}


def _f(name: str) -> str:
    return f"{_ROOT}/{name}"


def _entries(tmp_path: Path, files: dict[str, str], target: str, fn: str) -> list[tuple]:
    """A function's calls as written: a name may appear once per target file."""
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
    [f] = [f for f in parser.parse_file(ctx).functions if f.name == fn]
    return [(c.name, c.path) for c in f.calls]


# --- one entry per name and target file; an unresolved call once per name ---------------------

_SAME_NAME = """package com.acme;
import org.vertx.java.core.json.JsonObject;
public class Svc {
    void libraryFirst(JsonObject o) { o.getString("a"); JsonUtil.getString(o, "b"); }
    void repoFirst(JsonObject o) { JsonUtil.getString(o, "b"); o.getString("a"); }
    void libraryOnly(JsonObject o) { o.getString("a"); o.getString("b"); }
    void order(JsonObject o) { o.size(); o.getString("a"); Helper.util(); JsonUtil.getString(o, "b"); }
}
"""


_FILES = {"Svc.java": _SAME_NAME, "JsonUtil.java": _JSON_UTIL, "Helper.java": _HELPER}


def test_a_name_called_on_a_library_object_and_a_repo_class_is_two_entries(tmp_path: Path) -> None:
    # neither hides the other, whichever comes first
    assert _entries(tmp_path, _FILES, "Svc.java", "libraryFirst") == [
        ("getString", None), ("getString", _f("JsonUtil.java"))]
    assert _entries(tmp_path, _FILES, "Svc.java", "repoFirst") == [
        ("getString", _f("JsonUtil.java")), ("getString", None)]


def test_a_name_only_called_on_library_objects_stays_unresolved(tmp_path: Path) -> None:
    # two unresolved calls of one name: one entry
    assert _entries(tmp_path, _FILES, "Svc.java", "libraryOnly") == [("getString", None)]


def test_calls_keep_first_appearance_order(tmp_path: Path) -> None:
    assert _entries(tmp_path, _FILES, "Svc.java", "order") == [
        ("size", None), ("getString", None), ("util", _f("Helper.java")),
        ("getString", _f("JsonUtil.java"))]


def test_same_name_calls_to_different_files_are_each_kept(tmp_path: Path) -> None:
    src = """package com.acme;
public class Main {
    void start() { Helper.util(); Other.util(); Helper.util(); }
}
"""
    other = "package com.acme;\npublic class Other { public static void util() {} }\n"
    assert _entries(tmp_path, {"Main.java": src, "Helper.java": _HELPER, "Other.java": other},
                    "Main.java", "start") == [("util", _f("Helper.java")), ("util", _f("Other.java"))]


# --- `Outer.this.m()` -------------------------------------------------------------------------

_OUTER_THIS = """package com.acme;
import org.vertx.java.core.Handler;
import org.vertx.java.core.eventbus.Message;
public class Svc extends Parent {
    void anon() {
        vertx.eventBus().registerHandler("a", new Handler<Message<Object>>() {
            public void handle(Message<Object> m) { Svc.this.own(); }
        });
    }
    void inherited() {
        vertx.eventBus().registerHandler("b", new Handler<Message<Object>>() {
            public void handle(Message<Object> m) { Svc.this.fromParent(); }
        });
    }
    void qualified() { com.acme.Svc.this.own(); }
    void library() {
        vertx.eventBus().registerHandler("c", new Handler<Message<Object>>() {
            public void handle(Message<Object> m) { Svc.this.fromLibrary(); }
        });
    }
    class Inner { void call() { Svc.this.own(); } }
    void own() {}
}
"""
_PARENT = """package com.acme;
import org.lib.LibraryBase;
public class Parent extends LibraryBase { public void fromParent() {} }
"""


def test_outer_this_is_the_named_class(tmp_path: Path) -> None:
    calls = _calls(tmp_path, {"Svc.java": _OUTER_THIS, "Parent.java": _PARENT}, "Svc.java")
    assert calls["Svc$1.handle"] == {"own": _f("Svc.java")}
    assert "own" not in calls["anon"]  # the anonymous class's call is its method's, not anon()'s
    assert calls["qualified"] == {"own": _f("Svc.java")}
    assert calls["call"] == {"own": _f("Svc.java")}  # an inner class naming its outer class


def test_outer_this_reaches_an_in_repo_ancestor(tmp_path: Path) -> None:
    calls = _calls(tmp_path, {"Svc.java": _OUTER_THIS, "Parent.java": _PARENT}, "Svc.java")
    assert calls["Svc$2.handle"]["fromParent"] == _f("Parent.java")


def test_outer_this_with_a_method_from_a_library_base_stays_unresolved(tmp_path: Path) -> None:
    calls = _calls(tmp_path, {"Svc.java": _OUTER_THIS, "Parent.java": _PARENT}, "Svc.java")
    assert calls["Svc$3.handle"]["fromLibrary"] is None


# --- a field an in-repo base class declares ---------------------------------------------------

_CHILD = """package com.acme.web;
import com.acme.Base;
import org.vertx.java.core.Handler;
import org.vertx.java.core.eventbus.Message;
public class Child extends Base {
    void direct() { helper.inst(); }
    void viaThis() { this.helper.inst(); }
    void privateField() { hidden.inst(); }
    void libraryType() { client.inst(); }
    void localWins() { Other shadowed = null; shadowed.inst(); }
    void inRunnable() {
        submit(new Runnable() { public void run() { helper.inst(); } });
    }
    void inHandler() {
        vertx.eventBus().registerHandler("a", new Handler<Message<Object>>() {
            public void handle(Message<Object> m) { helper.inst(); }
        });
    }
    void thisInHandler() {
        vertx.eventBus().registerHandler("a", new Handler<Message<Object>>() {
            public void handle(Message<Object> m) { this.helper.inst(); }
        });
    }
    void inUnknownAnonymous() {
        run(new org.lib.Task() { public void go() { helper.inst(); } });
    }
}
"""
_OTHER = "package com.acme.web;\npublic class Other { public void inst() {} }\n"


def _child_calls(tmp_path: Path, base: str = _BASE) -> dict[str, dict[str, str | None]]:
    return _calls(tmp_path, {"Base.java": base, "Helper.java": _HELPER, "web/Child.java": _CHILD,
                             "web/Other.java": _OTHER}, "web/Child.java")


def test_a_field_of_an_in_repo_base_gives_its_type(tmp_path: Path) -> None:
    calls = _child_calls(tmp_path)
    assert calls["direct"] == {"inst": _f("Helper.java")}
    assert calls["viaThis"] == {"inst": _f("Helper.java")}


def test_a_private_or_library_typed_field_gives_nothing(tmp_path: Path) -> None:
    calls = _child_calls(tmp_path)
    assert calls["privateField"] == {"inst": None}
    assert calls["libraryType"] == {"inst": None}


def test_a_local_hides_the_inherited_field(tmp_path: Path) -> None:
    calls = _child_calls(tmp_path)
    assert calls["localWins"] == {"inst": _f("web/Other.java")}


def test_inside_an_anonymous_runnable_a_bare_name_is_the_outer_field(tmp_path: Path) -> None:
    calls = _child_calls(tmp_path)
    assert calls["Child$1.run"]["inst"] == _f("Helper.java")


def test_inside_a_framework_handler_the_inherited_field_is_not_resolved(tmp_path: Path) -> None:
    # A framework's handler interface is not known to the Java parser to declare no fields.
    calls = _child_calls(tmp_path)
    assert calls["Child$2.handle"]["inst"] is None


def test_inside_an_anonymous_class_this_field_is_not_the_outer_one(tmp_path: Path) -> None:
    calls = _child_calls(tmp_path)
    assert calls["Child$3.handle"]["inst"] is None


def test_inside_an_anonymous_class_of_unknown_type_the_field_is_not_resolved(tmp_path: Path) -> None:
    # `org.lib.Task` may declare a `helper` field of its own
    calls = _child_calls(tmp_path)
    assert calls["Child$4.go"]["inst"] is None


def test_the_nearest_declaring_class_decides(tmp_path: Path) -> None:
    middle = """package com.acme;
import org.lib.Client;
public abstract class Middle extends Base { protected Client helper; }
"""
    child = _CHILD.replace("import com.acme.Base;", "import com.acme.Middle;") \
        .replace("extends Base", "extends Middle")
    calls = _calls(tmp_path, {"Base.java": _BASE, "Middle.java": middle, "Helper.java": _HELPER,
                              "web/Child.java": child, "web/Other.java": _OTHER}, "web/Child.java")
    assert calls["direct"] == {"inst": None}  # Middle's library-typed `helper` hides Base's


def test_a_field_type_is_read_in_the_declaring_file(tmp_path: Path) -> None:
    # The child imports a library `Helper`; the field's type is what the base's file names.
    child = _CHILD.replace("public class Child", "import org.lib.Helper;\npublic class Child")
    calls = _calls(tmp_path, {"Base.java": _BASE, "Helper.java": _HELPER, "web/Child.java": child,
                              "web/Other.java": _OTHER}, "web/Child.java")
    assert calls["direct"] == {"inst": _f("Helper.java")}


def test_a_field_type_two_repository_classes_share_stays_unresolved(tmp_path: Path) -> None:
    other_helper = "package com.acme.web;\npublic class Helper { public void inst() {} }\n"
    calls = _calls(tmp_path, {"Base.java": _BASE, "Helper.java": _HELPER, "web/Child.java": _CHILD,
                              "web/Helper.java": other_helper, "web/Other.java": _OTHER},
                   "web/Child.java")
    assert calls["direct"] == {"inst": None}


def test_a_library_base_ends_the_field_lookup(tmp_path: Path) -> None:
    child = _CHILD.replace("import com.acme.Base;", "import org.lib.Base;")
    calls = _calls(tmp_path, {"Base.java": _BASE, "Helper.java": _HELPER, "web/Child.java": child,
                              "web/Other.java": _OTHER}, "web/Child.java")
    assert calls["direct"] == {"inst": None}


# --- a call in a field initializer belongs to no method ---------------------------------------

def test_a_call_in_a_field_initializer_is_not_recorded(tmp_path: Path) -> None:
    src = """package com.acme;
public class Addr { public static final String BUS = Helper.util2(); void m() {} }
"""
    calls = _calls(tmp_path, {"Addr.java": src, "Helper.java": _HELPER}, "Addr.java")
    assert calls == {"m": {}}


# --- which simple names mean a field-less interface in a file ---------------------------------

def test_fieldless_types_follow_the_imports() -> None:
    assert "Runnable" in fieldless_types([])  # java.lang
    assert "Callable" in fieldless_types(["java.util.concurrent.Callable"])
    assert "Callable" in fieldless_types(["java.util.concurrent.*"])
    assert "Callable" not in fieldless_types([])
    assert "Runnable" not in fieldless_types(["com.lib.Runnable"])
    assert "Callable" not in fieldless_types(["com.lib.Callable", "java.util.concurrent.*"])
    assert "Handler" not in fieldless_types(["org.vertx.java.core.Handler"])  # a framework's


def test_the_inherited_field_tier_is_off_unless_a_language_turns_it_on() -> None:
    from breezeai_cog.parsers.callresolve import make_resolver
    from breezeai_cog.parsers.index_common import ClassHeritage

    heritage = {
        "Base": ClassHeritage(extends=None, decorators=[], fields={"helper": "Helper"}),
        "Child": ClassHeritage(extends="Base", decorators=[]),
        "Helper": ClassHeritage(extends=None, decorators=[], methods={"inst": "Helper.java"}),
    }
    for on, expected in ((False, None), (True, "Helper.java")):
        resolve = make_resolver({}, set(), "Child.java", heritage=heritage, declared_only=True,
                                inherited_fields=on)
        assert resolve("inst", "helper", "Child", {}) == expected


# --- end to end, so the index reaches the workers ---------------------------------------------

@pytest.mark.parametrize("jobs", [1, 2])
def test_inherited_fields_reach_the_workers_for_any_jobs(tmp_path: Path, jobs: int) -> None:
    from breezeai_cog import analyze_repo

    repo = tmp_path / "repo"
    for rel, src in {"Base.java": _BASE, "Helper.java": _HELPER, "web/Child.java": _CHILD,
                     "web/Other.java": _OTHER}.items():
        p = repo / _ROOT / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)
    result = analyze_repo(repo, out=tmp_path / "out", jobs=jobs)
    lines = gzip.open(result.out_path, "rt", encoding="utf-8").read().splitlines()
    rec = next(json.loads(line) for line in lines if '"path": "' + _f("web/Child.java") in line
               or f'"path":"{_f("web/Child.java")}"' in line)
    calls = {f["id"].rsplit("#", 2)[-2] + "." + f["name"]: {c["name"]: c.get("path") for c in f["calls"]}
             for f in rec["functions"]}
    assert calls["Child.direct"] == {"inst": _f("Helper.java")}
    assert calls["Child$1.run"]["inst"] == _f("Helper.java")


def test_a_shared_field_type_name_keeps_the_answer_the_file_itself_gives(tmp_path: Path) -> None:
    # Two repository classes are named Helper, so the inherited field's type cannot be looked
    # up by name; the file's own declaration of `helper` (its import says which Helper) decides.
    other_helper = "package com.acme.web;\npublic class Helper { public void inst() {} }\n"
    child = """package com.acme.other;
import com.acme.Base;
import com.acme.Helper;
public class Child extends Base {
    Child(Helper helper) { this.helper = helper; }
    void use() { helper.inst(); }
}
"""
    calls = _calls(tmp_path, {"Base.java": _BASE, "Helper.java": _HELPER,
                              "web/Helper.java": other_helper, "other/Child.java": child},
                   "other/Child.java")
    assert calls["use"] == {"inst": _f("Helper.java")}


def test_an_inherited_field_is_not_typed_by_another_methods_parameter(tmp_path: Path) -> None:
    # `svc` in `second` and `third` is Base's field (a Helper), not `first`'s parameter.
    base = "package com.acme;\npublic class Base { protected Helper svc; }\n"
    other = "package com.acme;\npublic class Other { public void inst() {} public void only() {} }\n"
    child = """package com.acme;
public class Child extends Base {
    void first(Other svc) { svc.only(); }
    void second() { svc.inst(); }
    void third() { svc.only(); }
}
"""
    calls = _calls(tmp_path, {"Base.java": base, "Helper.java": _HELPER, "Other.java": other,
                              "Child.java": child}, "Child.java")
    assert calls["first"] == {"only": _f("Other.java")}
    assert calls["second"] == {"inst": _f("Helper.java")}
    assert calls["third"] == {"only": None}  # Helper declares no `only`


# --- a variable typed `Outer.Inner` is that nested class, never this file's `Inner` -----------

_GETTER = """package com.acme;
public class Getter {
    public static class Context { public void setAppId(String id) {} }
    public static class Box<T> { public void put(T v) {} }
}
"""
_HANDLER = """package com.acme;
public class Handler {
    static class Context { Context(Object body) {} void own() {} }
    void qualified() { Getter.Context c = new Getter.Context(); c.setAppId("A1"); }
    void simple() { Context c = new Context(null); c.own(); }
    void undeclared() { Getter.Context c = new Getter.Context(); c.own(); }
    void generic() { Getter.Box<String> b = new Getter.Box<>(); b.put("x"); }
    void ownOuter() { Handler.Context c = new Handler.Context(null); c.own(); }
}
"""


def test_a_qualified_nested_type_resolves_through_its_outer_class(tmp_path: Path) -> None:
    calls = _calls(tmp_path, {"Getter.java": _GETTER, "Handler.java": _HANDLER}, "Handler.java")
    assert calls["qualified"] == {"setAppId": _f("Getter.java")}  # same package, no import
    assert calls["generic"] == {"put": _f("Getter.java")}
    assert calls["ownOuter"] == {"own": _f("Handler.java")}  # `Handler.Context` is this file's
    # a simple name keeps today's rule; `new Context(…)` is this file's nested class's constructor
    assert calls["simple"] == {"Context": _f("Handler.java"), "own": _f("Handler.java")}


def test_a_method_the_nested_class_does_not_declare_has_no_path(tmp_path: Path) -> None:
    # Getter.Context declares no `own`; this file's Context does, but is another class.
    calls = _calls(tmp_path, {"Getter.java": _GETTER, "Handler.java": _HANDLER}, "Handler.java")
    assert calls["undeclared"] == {"own": None}


def test_an_imported_outer_class_gives_its_file(tmp_path: Path) -> None:
    getter = _GETTER.replace("package com.acme;", "package com.acme.api;")
    handler = _HANDLER.replace("package com.acme;", "package com.acme;\nimport com.acme.api.Getter;")
    calls = _calls(tmp_path, {"api/Getter.java": getter, "Handler.java": handler}, "Handler.java")
    assert calls["qualified"] == {"setAppId": _f("api/Getter.java")}


def test_a_library_outer_class_never_gives_this_file(tmp_path: Path) -> None:
    # this file's own Context declares `setAppId`, but `Getter.Context` is the library's
    handler = """package com.acme;
import org.lib.Getter;
import java.util.Map;
public class Handler {
    static class Context { void setAppId(String s) {} }
    static class Entry { Object getKey() { return null; } }
    void run(Map.Entry<String, String> e) {
        Getter.Context c = new Getter.Context();
        c.setAppId("A1");
        e.getKey();
    }
}
"""
    calls = _calls(tmp_path, {"Handler.java": handler}, "Handler.java")
    assert calls["run"] == {"setAppId": None, "getKey": None}


def test_a_call_on_a_qualified_nested_receivers_call_needs_its_outer_class(tmp_path: Path) -> None:
    # `c.self()` returns a Context: only the library's Getter.Context could say what that is
    handler = """package com.acme;
import org.lib.Getter;
public class Handler {
    static class Context { Context self() { return this; } void setAppId(String s) {} }
    void run() { Getter.Context c = new Getter.Context(); c.self().setAppId("A1"); }
}
"""
    calls = _calls(tmp_path, {"Handler.java": handler}, "Handler.java")
    assert calls["run"] == {"self": None, "setAppId": None}


# --- a type written with its package is the class that package and name give -------------------

_API = {
    "api/Getter.java": "package com.acme.api;\n"
    "public class Getter { public static class Context { public void setAppId(String s) {} } }\n",
    "api/Order.java": "package com.acme.api;\npublic class Order { public int total() { return 1; } }\n",
    "Order.java": "package com.acme;\npublic class Order { public int total() { return 2; } }\n",
}
_QUALIFIED = """package com.acme;
public class Handler {
    static class Context { void setAppId(String s) {} }
    static class Entry { Object getKey() { return null; } }
    void lib(java.util.Map.Entry<String, String> e) { e.getKey(); }
    void nested(com.acme.api.Getter.Context c) { c.setAppId("x"); }
    void top(com.acme.api.Order o) { o.total(); }
    void list(java.util.List<String> l) { l.add("x"); }
}
"""


def test_a_package_qualified_receiver_is_the_class_its_package_names(tmp_path: Path) -> None:
    calls = _calls(tmp_path, {**_API, "Handler.java": _QUALIFIED}, "Handler.java")
    assert calls["lib"] == {"getKey": None}  # a library type, never this file's Entry
    assert calls["nested"] == {"setAppId": _f("api/Getter.java")}  # not this file's Context
    assert calls["top"] == {"total": _f("api/Order.java")}  # not the same-package Order
    assert calls["list"] == {"add": None}


def test_a_nested_class_with_no_method_of_its_own_reads_its_ancestor(tmp_path: Path) -> None:
    files = {
        "Base.java": "package com.acme;\npublic class Base { public void go() {} }\n",
        "Outer.java": "package com.acme;\n"
        "public class Outer { public static class Inner extends Base { } }\n",
        "Handler.java": "package com.acme;\n"
        "public class Handler { void run() { Outer.Inner i = new Outer.Inner(); i.go(); } }\n",
    }
    assert _calls(tmp_path, files, "Handler.java")["run"] == {"go": _f("Base.java")}


def test_a_nested_class_the_outer_class_does_not_declare_has_no_path(tmp_path: Path) -> None:
    # `Outer.Inner` names a class Outer inherits (declared in Base): not resolved through Outer
    files = {
        "Base.java": "package com.acme;\n"
        "public class Base { public static class Inner { public void go() {} } }\n",
        "Outer.java": "package com.acme;\npublic class Outer extends Base { }\n",
        "Handler.java": "package com.acme;\n"
        "public class Handler { void run(Outer.Inner i) { i.go(); } }\n",
    }
    assert _calls(tmp_path, files, "Handler.java")["run"] == {"go": None}
