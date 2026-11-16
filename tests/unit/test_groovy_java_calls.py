"""Groovy code sees the repository's Java classes (``related_languages = ("java",)``): an import of
one is an in-repo import, a call on one resolves to the file that declares the method, and a
Groovy class extending one inherits its methods. A simple name both languages declare is
ambiguous; Java code does not see Groovy classes."""

from __future__ import annotations

from pathlib import Path

from breezeai_cog import iter_file_records
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.groovy.parser import GroovyParser
from breezeai_cog.parsers.java.parser import JavaParser
from breezeai_cog.schemas import FileRecord

_G = "src/main/groovy/com/acme"
_J = "src/main/java/com/acme"


def _parse(tmp_path: Path, files: dict[str, str], target: str, *, java: bool = True) -> FileRecord:
    for rel, src in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)
    groovy = GroovyParser().build_index(tmp_path, list(tmp_path.rglob("*.groovy")))
    related = {"java": JavaParser().build_index(tmp_path, list(tmp_path.rglob("*.java")))} if java else {}
    path = tmp_path / target
    ctx = ParseContext(path=target, abs_path=path, source=path.read_bytes(), repo_root=tmp_path,
                       resolution_index=groovy, related_indexes=related)
    return GroovyParser().parse_file(ctx)


def _calls(record: FileRecord) -> dict[str, dict[str, str | None]]:
    return {f.name: {c.name: c.path for c in f.calls} for f in record.functions}


_CLIENT = """package com.acme.api;
public class DataApiClient extends ApiClient {
    public Object get(String table) { return null; }
}
"""
_API = """package com.acme.api;
public class ApiClient {
    public void send(String url) {}
}
"""
_SCRIPT = """package com.acme
import com.acme.api.DataApiClient
class Main {
    void go() {
        DataApiClient dataApi = new DataApiClient()
        dataApi.get('orders')
        dataApi.send('/x')
        dataApi.close()
    }
}
"""


def test_an_import_of_a_java_class_is_in_the_repository(tmp_path: Path) -> None:
    files = {f"{_J}/api/DataApiClient.java": _CLIENT, f"{_J}/api/ApiClient.java": _API,
             f"{_G}/Main.groovy": _SCRIPT}
    record = _parse(tmp_path, files, f"{_G}/Main.groovy")
    assert record.importFiles == [f"{_J}/api/DataApiClient.java"]
    assert record.externalImports == []
    # declared → its file; inherited → the Java ancestor's; declared nowhere → null
    assert _calls(record)["go"] == {"get": f"{_J}/api/DataApiClient.java",
                                    "send": f"{_J}/api/ApiClient.java", "close": None}


def test_without_a_java_index_the_import_stays_external(tmp_path: Path) -> None:
    files = {f"{_J}/api/DataApiClient.java": _CLIENT, f"{_G}/Main.groovy": _SCRIPT}
    record = _parse(tmp_path, files, f"{_G}/Main.groovy", java=False)
    assert record.importFiles == []
    assert record.externalImports == ["com.acme.api.DataApiClient"]
    assert _calls(record)["go"] == {"get": None, "send": None, "close": None}


def test_a_java_class_of_the_same_package_needs_no_import(tmp_path: Path) -> None:
    files = {
        f"{_J}/Util.java": "package com.acme;\npublic class Util {\n"
                           "    public static String trim(String s) { return s; }\n}\n",
        f"{_G}/Use.groovy": "package com.acme\nclass Use {\n"
                            "    void run() { Util.trim('a'); Util.pad('a') }\n}\n",
    }
    assert _calls(_parse(tmp_path, files, f"{_G}/Use.groovy"))["run"] == {
        "trim": f"{_J}/Util.java", "pad": None}


_BATCH = """package com.acme.batch;
public abstract class AbstractBatch {
    public void run(Object context) {}
}
"""


def test_a_groovy_class_extending_a_java_class_inherits_its_methods(tmp_path: Path) -> None:
    files = {
        f"{_J}/batch/AbstractBatch.java": _BATCH,
        f"{_G}/Job.groovy": "package com.acme\nimport com.acme.batch.AbstractBatch\n"
                            "class Job extends AbstractBatch {\n"
                            "    void prepare() { run(null); this.run(null); missing() }\n}\n",
        f"{_G}/Main.groovy": "package com.acme\nclass Main {\n"
                             "    void go() { Job proc = new Job(); proc.run(null); proc.prepare() }\n}\n",
    }
    assert _calls(_parse(tmp_path, files, f"{_G}/Main.groovy"))["go"] == {
        "run": f"{_J}/batch/AbstractBatch.java", "prepare": f"{_G}/Job.groovy"}
    assert _calls(_parse(tmp_path, files, f"{_G}/Job.groovy"))["prepare"] == {
        "run": f"{_J}/batch/AbstractBatch.java", "missing": None}


def test_a_library_parent_still_ends_the_chain(tmp_path: Path) -> None:
    # `AbstractBatch` is imported from a library; the repository's Java one is another class.
    files = {
        f"{_J}/batch/AbstractBatch.java": _BATCH,
        f"{_G}/Job.groovy": "package com.acme\nimport org.lib.AbstractBatch\n"
                            "class Job extends AbstractBatch { }\n",
        f"{_G}/Main.groovy": "package com.acme\nclass Main {\n"
                             "    void go(Job proc) { proc.run(null) }\n}\n",
    }
    assert _calls(_parse(tmp_path, files, f"{_G}/Main.groovy"))["go"] == {"run": None}


def test_a_name_both_languages_declare_is_looked_up_in_the_imported_file(tmp_path: Path) -> None:
    files = {
        f"{_J}/api/Report.java": "package com.acme.api;\npublic class Report extends Base {\n"
                                 "    public void print() {}\n}\n",
        f"{_J}/api/Base.java": "package com.acme.api;\npublic class Base { public void render() {} }\n",
        f"{_G}/Report.groovy": "package com.acme\nclass Report {\n    void render() { }\n"
                               "    String title\n}\n",
        f"{_G}/Use.groovy": "package com.acme\nimport com.acme.api.Report as ApiReport\nclass Use {\n"
                            "    void a(ApiReport r) { r.print(); r.render() }\n"
                            "    void b(Report r) { r.render(); r.getTitle(); r.print() }\n}\n",
    }
    calls = _calls(_parse(tmp_path, files, f"{_G}/Use.groovy"))
    # the Java Report declares print(); render() is its parent's, which the name cannot reach
    assert calls["a"] == {"print": f"{_J}/api/Report.java", "render": None}
    # the same-package Groovy Report: its own method and generated accessor, not Java's print()
    assert calls["b"] == {"render": f"{_G}/Report.groovy", "getTitle": f"{_G}/Report.groovy",
                          "print": None}


def test_the_pipeline_hands_groovy_the_java_index_one_way(tmp_path: Path) -> None:
    files = {f"{_J}/api/DataApiClient.java": _CLIENT, f"{_J}/api/ApiClient.java": _API,
             f"{_G}/Main.groovy": _SCRIPT,
             f"{_J}/api/Caller.java": "package com.acme.api;\nimport com.acme.Main;\n"
                                      "public class Caller { void x(Main m) { m.go(); } }\n"}
    for rel, src in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)
    records = {r.path: r for r in iter_file_records(tmp_path)}
    main = records[f"{_G}/Main.groovy"]
    assert main.importFiles == [f"{_J}/api/DataApiClient.java"]
    assert _calls(main)["go"]["get"] == f"{_J}/api/DataApiClient.java"
    caller = records[f"{_J}/api/Caller.java"]  # Java does not look into the Groovy index
    assert caller.importFiles == [] and caller.externalImports == ["com.acme.Main"]


def test_a_name_both_languages_declare_means_the_groovy_class_unless_bound_to_java(
    tmp_path: Path,
) -> None:
    # Two unrelated `Command` classes: the Groovy one (with a Groovy parent) and a Java one in
    # another module. Groovy code that does not bind the name to Java keeps the Groovy class and
    # its chain; a Java class whose parent is the Java `Command` ends its chain there.
    files = {
        f"{_G}/cli/Base.groovy": "package com.acme.cli\nclass Base {\n    void complete() { }\n}\n",
        f"{_G}/cli/Command.groovy": "package com.acme.cli\nclass Command extends Base {\n"
                                    "    void validate() { }\n}\n",
        f"{_G}/cli/PluginCommand.groovy": "package com.acme.cli\nclass PluginCommand extends Command {\n"
                                          "    void run() { validate(); complete(); super.validate() }\n}\n",
        f"{_J}/forge/Command.java": "package com.acme.forge;\npublic class Command {\n"
                                   "    public void execute() {}\n}\n",
        f"{_J}/forge/AppCommand.java": "package com.acme.forge;\npublic class AppCommand extends Command {\n"
                                      "    public void create() {}\n}\n",
        f"{_G}/cli/Use.groovy": "package com.acme.cli\nimport com.acme.forge.AppCommand\nclass Use {\n"
                                "    void a(Command c) { c.validate(); c.complete() }\n"
                                "    void b(AppCommand c) { c.create(); c.execute() }\n}\n",
    }
    calls = _calls(_parse(tmp_path, files, f"{_G}/cli/PluginCommand.groovy"))
    assert calls["run"] == {"validate": f"{_G}/cli/Command.groovy",
                            "complete": f"{_G}/cli/Base.groovy"}
    calls = _calls(_parse(tmp_path, files, f"{_G}/cli/Use.groovy"))
    assert calls["a"] == {"validate": f"{_G}/cli/Command.groovy", "complete": f"{_G}/cli/Base.groovy"}
    # execute() is the Java Command's, a name the Groovy class shares: not followed
    assert calls["b"] == {"create": f"{_J}/forge/AppCommand.java", "execute": None}


def test_a_bare_call_both_statically_imported_and_declared_by_the_class_is_empty(
    tmp_path: Path,
) -> None:
    # Groovy picks the class's block(Closure) or the imported block(Scope, List) by the arguments.
    files = {
        f"{_J}/tools/Utils.java": "package com.acme.tools;\npublic class Utils {\n"
                                  "    public static Object block(Object scope, Object list) { return null; }\n"
                                  "    public static Object constX(Object v) { return null; }\n}\n",
        f"{_G}/Compiler.groovy": "package com.acme\nimport static com.acme.tools.Utils.block\n"
                                 "import static com.acme.tools.Utils.constX\nclass Compiler {\n"
                                 "    void block(Closure c) { block(null, []) }\n"
                                 "    void run() { block {}; constX(1) }\n}\n",
    }
    calls = _calls(_parse(tmp_path, files, f"{_G}/Compiler.groovy"))
    assert calls["block"] == {"block": None}
    assert calls["run"] == {"block": None, "constX": f"{_J}/tools/Utils.java"}


def test_the_static_import_rule_holds_in_a_class_with_a_parse_error(tmp_path: Path) -> None:
    files = {
        f"{_J}/tools/Utils.java": "package com.acme.tools;\npublic class Utils {\n"
                                  "    public static Object block(Object scope, Object list) { return null; }\n}\n",
        # the typed local with no initializer is a grammar gap: the class body has a parse error
        f"{_G}/Compiler.groovy": "package com.acme\nimport static com.acme.tools.Utils.block\nclass Compiler {\n"
                                 "    void block(Closure c) { block(null, []) }\n"
                                 "    void load() {\n        Map<String, String> names\n"
                                 "        try { names = [:] } catch (Exception e) { }\n    }\n}\n",
    }
    assert _calls(_parse(tmp_path, files, f"{_G}/Compiler.groovy"))["block"] == {"block": None}
