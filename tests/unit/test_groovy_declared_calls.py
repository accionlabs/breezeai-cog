"""A Groovy call on a typed variable or an imported class resolves to the file of the class, in-repo
ancestor or trait that declares the method, a property's generated accessor included, and is
empty when none does."""

from __future__ import annotations

from pathlib import Path

from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.groovy.parser import GroovyParser

_ROOT = "src/main/groovy/com/acme"


def _calls(tmp_path: Path, files: dict[str, str], target: str) -> dict[str, dict[str, str | None]]:
    for rel, src in files.items():
        p = tmp_path / _ROOT / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)
    parser = GroovyParser()
    index = parser.build_index(tmp_path, list(tmp_path.rglob("*.groovy")))
    rel = f"{_ROOT}/{target}"
    path = tmp_path / rel
    ctx = ParseContext(path=rel, abs_path=path, source=path.read_bytes(), repo_root=tmp_path,
                       resolution_index=index)
    record = parser.parse_file(ctx)
    return {f.name: {c.name: c.path for c in f.calls} for f in record.functions}


_BASE = """package com.acme
class BaseContext {
    void audit() { }
}
"""
_TRAIT = """package com.acme
trait Auditable {
    String tag
    void stamp() { }
}
"""
_CONTEXT = """package com.acme
import com.lib.BatchParameter
class BatchContext extends BaseContext implements Auditable {
    String jobName
    boolean dryRun
    Boolean boxed
    final String region = 'eu'
    private String secret
    static String shared
    String aURL
    void validate() { }
}
"""
_LIB_CONTEXT = """package com.acme
import com.lib.BatchParameter
class LibContext extends BatchParameter {
    void validate() { }
}
"""
_JOB = """package com.acme
class Job {
    void declared(BatchContext context) {
        context.validate()
        context.audit()
        context.stamp()
    }
    void library(LibContext lib) {
        lib.getPlatformId()
        lib.validate()
    }
    void properties(BatchContext context) {
        context.getJobName()
        context.setJobName('a')
        context.isDryRun()
        context.getDryRun()
        context.getShared()
        context.getaURL()
        context.getTag()
    }
    void notGenerated(BatchContext context) {
        context.isBoxed()
        context.setRegion('us')
        context.getSecret()
        context.save()
    }
}
"""
_FILES = {"BaseContext.groovy": _BASE, "Auditable.groovy": _TRAIT,
          "BatchContext.groovy": _CONTEXT, "LibContext.groovy": _LIB_CONTEXT, "Job.groovy": _JOB}


def test_a_call_resolves_to_the_class_ancestor_or_trait_that_declares_it(tmp_path: Path) -> None:
    calls = _calls(tmp_path, _FILES, "Job.groovy")["declared"]
    assert calls == {"validate": f"{_ROOT}/BatchContext.groovy",
                     "audit": f"{_ROOT}/BaseContext.groovy",
                     "stamp": f"{_ROOT}/Auditable.groovy"}


def test_a_method_from_a_library_base_class_has_no_path(tmp_path: Path) -> None:
    calls = _calls(tmp_path, _FILES, "Job.groovy")["library"]
    assert calls == {"getPlatformId": None, "validate": f"{_ROOT}/LibContext.groovy"}


def test_a_property_accessor_resolves_to_the_file_declaring_the_property(tmp_path: Path) -> None:
    calls = _calls(tmp_path, _FILES, "Job.groovy")["properties"]
    context = f"{_ROOT}/BatchContext.groovy"
    assert calls == {"getJobName": context, "setJobName": context, "isDryRun": context,
                     "getDryRun": context, "getShared": context, "getaURL": context,
                     "getTag": f"{_ROOT}/Auditable.groovy"}


def test_an_accessor_groovy_does_not_generate_has_no_path(tmp_path: Path) -> None:
    # a boxed Boolean has no `is` getter, a final property no setter, a private field no
    # accessors, and GORM's save() is added at runtime
    calls = _calls(tmp_path, _FILES, "Job.groovy")["notGenerated"]
    assert calls == {"isBoxed": None, "setRegion": None, "getSecret": None, "save": None}


def test_a_static_call_on_an_imported_class_needs_the_method_declared(tmp_path: Path) -> None:
    files = {
        "domain/Book.groovy": "package com.acme.domain\nclass Book {\n"
                              "    static Book of(String t) { new Book() }\n}\n",
        "domain/Color.groovy": "package com.acme.domain\nenum Color { RED }\n",
        "Shop.groovy": "package com.acme\nimport com.acme.domain.Book\nimport com.acme.domain.Color\n"
                       "class Shop {\n    void run() {\n        Book.of('x')\n"
                       "        Book.findByTitle('x')\n        Color.valueOf('RED')\n    }\n}\n",
    }
    calls = _calls(tmp_path, files, "Shop.groovy")["run"]
    assert calls == {"of": f"{_ROOT}/domain/Book.groovy", "findByTitle": None, "valueOf": None}


def test_two_traits_declaring_one_method_leave_it_empty(tmp_path: Path) -> None:
    files = {
        "A.groovy": "package com.acme\ntrait A { void ping() { } }\n",
        "B.groovy": "package com.acme\ntrait B { void ping() { } }\n",
        "C.groovy": "package com.acme\nclass C implements A, B { void own() { } }\n",
        "D.groovy": "package com.acme\nclass D implements A, B { void ping() { } }\n",
        "Use.groovy": "package com.acme\nclass Use {\n"
                      "    void onC(C c) { c.ping() }\n    void onD(D d) { d.ping() }\n}\n",
    }
    calls = _calls(tmp_path, files, "Use.groovy")
    assert calls["onC"] == {"ping": None}
    # the class's own method wins over its traits'
    assert calls["onD"] == {"ping": f"{_ROOT}/D.groovy"}


def test_a_class_name_two_files_share_keeps_the_type_file(tmp_path: Path) -> None:
    files = {
        "a/Report.groovy": "package com.acme.a\nclass Report { void print() { } }\n",
        "b/Report.groovy": "package com.acme.b\nclass Report { }\n",
        "Use.groovy": "package com.acme\nimport com.acme.b.Report\nclass Use {\n"
                      "    void run(Report r) {\n        r.render()\n    }\n}\n",
    }
    calls = _calls(tmp_path, files, "Use.groovy")["run"]
    assert calls == {"render": f"{_ROOT}/b/Report.groovy"}


def test_a_class_whose_body_does_not_parse_keeps_the_type_file(tmp_path: Path) -> None:
    # The grammar has no explicit type arguments on a call (`Util.<String> m()`); the error it
    # leaves in the enum body may hide members, so its methods are not known.
    files = {
        "Mode.groovy": "package com.acme\nenum Mode {\n    ON, OFF\n"
                       "    private static Map<String, String> names = Util.<String, String> newMap('a', 'b')\n"
                       "    static boolean isDev() { true }\n}\n",
        "Use.groovy": "package com.acme\nclass Use {\n    void run() { Mode.isDev() }\n}\n",
    }
    assert _calls(tmp_path, files, "Use.groovy")["run"] == {"isDev": f"{_ROOT}/Mode.groovy"}


def test_an_interface_method_resolves_to_the_parent_interface_declaring_it(tmp_path: Path) -> None:
    files = {
        "Reads.groovy": "package com.acme\ninterface Reads { Object get(Serializable id) }\n",
        "Writes.groovy": "package com.acme\ninterface Writes { void save(Object o) }\n",
        "Store.groovy": "package com.acme\ninterface Store extends Reads, Writes { void flush() }\n",
        "Use.groovy": "package com.acme\nclass Use {\n"
                      "    void run(Store s) {\n        s.get(1)\n        s.save(2)\n        s.flush()\n"
                      "        s.close()\n    }\n}\n",
    }
    assert _calls(tmp_path, files, "Use.groovy")["run"] == {
        "get": f"{_ROOT}/Reads.groovy", "save": f"{_ROOT}/Writes.groovy",
        "flush": f"{_ROOT}/Store.groovy", "close": None}


def test_a_parse_error_inside_a_method_makes_the_class_methods_unknown(tmp_path: Path) -> None:
    # The grammar has no typed local without an initializer (`Map<…> names`); the error it leaves
    # in load() reads the `catch` as a constructor and drops find() from the tree, with no ERROR
    # among the class's members.
    files = {
        "Registry.groovy": "package com.acme\nclass Registry {\n"
                           "    void load() {\n        Map<String, List<String>> names\n"
                           "        try {\n            names = read()\n"
                           "        } catch (Exception e) {\n            names = [:]\n        }\n    }\n"
                           "    String find(String name) { name }\n}\n",
        "Use.groovy": "package com.acme\nclass Use {\n    void run(Registry r) { r.find('x') }\n}\n",
    }
    assert _calls(tmp_path, files, "Use.groovy")["run"] == {"find": f"{_ROOT}/Registry.groovy"}


def test_a_method_the_grammar_splits_into_a_constructor_makes_the_methods_unknown(tmp_path: Path) -> None:
    # `default String banner()` reads as a field typed `default` and a constructor `banner`,
    # with no ERROR, so the interface's methods are not known.
    files = {
        "LifeCycle.groovy": "package com.acme\ninterface LifeCycle {\n    void start()\n"
                            "    default String banner() { 'x' }\n}\n",
        "Use.groovy": "package com.acme\nclass Use {\n    void run(LifeCycle l) { l.banner() }\n}\n",
    }
    assert _calls(tmp_path, files, "Use.groovy")["run"] == {"banner": f"{_ROOT}/LifeCycle.groovy"}


def test_a_call_on_an_import_alias_is_looked_up_as_the_class(tmp_path: Path) -> None:
    files = {
        "util/Env.groovy": "package com.acme.util\nclass Env {\n    static boolean isWar() { false }\n}\n",
        "Use.groovy": "package com.acme\nimport com.acme.util.Env as AppEnv\nclass Use {\n"
                      "    void run(AppEnv e) {\n        AppEnv.isWar()\n        AppEnv.isJar()\n"
                      "        e.isWar()\n    }\n}\n",
    }
    calls = _calls(tmp_path, files, "Use.groovy")["run"]
    assert calls == {"isWar": f"{_ROOT}/util/Env.groovy", "isJar": None}


def test_a_subclass_of_a_class_whose_methods_are_not_known_keeps_the_type_file(tmp_path: Path) -> None:
    files = {
        "Builder.groovy": "package com.acme\nclass Builder {\n"
                          "    private static Map<String, String> m = Util.<String, String> newMap()\n"
                          "    Object build() { null }\n}\n",
        "MongoBuilder.groovy": "package com.acme\nclass MongoBuilder extends Builder { }\n",
        "Use.groovy": "package com.acme\nclass Use {\n    void run(MongoBuilder b) { b.build() }\n}\n",
    }
    assert _calls(tmp_path, files, "Use.groovy")["run"] == {"build": f"{_ROOT}/MongoBuilder.groovy"}


_PARENT = "package com.acme\nclass Parent {\n    void audit() { }\n    void shared() { }\n}\n"


def test_a_bare_this_or_super_call_resolves_to_the_ancestor_declaring_it(tmp_path: Path) -> None:
    files = {
        "Parent.groovy": _PARENT,
        "Kid.groovy": "package com.acme\nclass Kid extends Parent {\n    void shared() { }\n"
                      "    void bare() { audit() }\n    void self() { this.audit() }\n"
                      "    void up() { super.shared() }\n    void own() { shared() }\n"
                      "    void none() { missing() }\n}\n",
    }
    calls = _calls(tmp_path, files, "Kid.groovy")
    parent, kid = f"{_ROOT}/Parent.groovy", f"{_ROOT}/Kid.groovy"
    assert calls["bare"] == {"audit": parent}
    assert calls["self"] == {"audit": parent}
    assert calls["up"] == {"shared": parent}
    assert calls["own"] == {"shared": kid}  # the class's own method wins over the parent's
    assert calls["none"] == {"missing": None}


def test_a_bare_call_to_a_trait_method_or_own_accessor_resolves_to_its_declaring_file(
    tmp_path: Path,
) -> None:
    files = {
        "Parent.groovy": "package com.acme\nclass Parent {\n    void stamp() { }\n}\n",
        "Stamps.groovy": "package com.acme\ntrait Stamps {\n    void stamp() { }\n}\n",
        "Doc.groovy": "package com.acme\nclass Doc extends Parent implements Stamps {\n"
                      "    String title\n    void run() {\n        stamp()\n        getTitle()\n    }\n}\n",
    }
    # a trait's method is the class's own: it wins over the parent's
    assert _calls(tmp_path, files, "Doc.groovy")["run"] == {
        "stamp": f"{_ROOT}/Stamps.groovy", "getTitle": f"{_ROOT}/Doc.groovy"}


def test_a_method_of_a_library_parent_has_no_path(tmp_path: Path) -> None:
    files = {"Job.groovy": "package com.acme\nimport com.lib.Script\nclass Job extends Script {\n"
                           "    void run() { binding() }\n}\n"}
    assert _calls(tmp_path, files, "Job.groovy")["run"] == {"binding": None}


def test_a_call_in_an_anonymous_class_is_not_the_enclosing_class_s(tmp_path: Path) -> None:
    files = {
        "Parent.groovy": _PARENT,
        "Kid.groovy": "package com.acme\nclass Kid extends Parent {\n    void run() {\n"
                      "        def r = new Runnable() {\n            void run() { audit() }\n        }\n"
                      "    }\n}\n",
    }
    assert _calls(tmp_path, files, "Kid.groovy")["run"]["audit"] is None
