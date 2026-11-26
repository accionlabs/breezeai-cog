"""A Groovy class's code outside its methods — field initializers (a closure stored in a field
included), ``static { … }`` and ``{ … }`` blocks, enum constant arguments — keeps its calls in
``Class.calls``, resolved like a method's."""

from __future__ import annotations

from pathlib import Path

from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.groovy.parser import GroovyParser

_ROOT = "src/main/groovy/com/acme"


def _classes(tmp_path: Path, files: dict[str, str], target: str):
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
    return ({c.name: [(x.name, x.path) for x in c.calls or []] for c in record.classes},
            {f.name: [x.name for x in f.calls] for f in record.functions})


_CONFIG = """package com.acme
class Config {
    static String getValue(String a, String b) { a }
}
"""


def test_initializers_closures_and_blocks_are_the_class_s_calls(tmp_path: Path) -> None:
    classes, functions = _classes(tmp_path, {"Config.groovy": _CONFIG, "Mailer.groovy": """package com.acme
import org.slf4j.LoggerFactory
class Mailer {
    private static log = LoggerFactory.getLogger(Mailer.class)
    private static final String APP = Config.getValue("app", "id")
    def done = { x -> report(x) }
    static { load() }
    { reset() }
    void send() { format() }
    static void load() { }
    void reset() { }
    void report(x) { }
    void format() { }
}
"""}, "Mailer.groovy")
    own = f"{_ROOT}/Mailer.groovy"
    assert classes["Mailer"] == [
        ("getLogger", None),
        ("getValue", f"{_ROOT}/Config.groovy"),
        ("report", own),
        ("load", own),
        ("reset", own),
    ]
    # a method keeps its own calls; none of the class's move into it
    assert functions["send"] == ["format"]


def test_enum_constant_arguments_and_nested_class(tmp_path: Path) -> None:
    classes, _ = _classes(tmp_path, {"Config.groovy": _CONFIG, "Kind.groovy": """package com.acme
enum Kind {
    A(Config.getValue("a", "b")), B("b")
    Kind(String s) { init(s) }
    static void init(String s) { }
}
class Holder {
    static class Inner {
        String v = Config.getValue("i", "j")
    }
    void m() { }
}
"""}, "Kind.groovy")
    assert classes["Kind"] == [("getValue", f"{_ROOT}/Config.groovy")]  # not the constructor's init
    assert classes["Holder"] == []  # the inner class's initializer is its own
    assert classes["Inner"] == [("getValue", f"{_ROOT}/Config.groovy")]


def test_a_class_with_no_code_outside_methods_has_no_calls(tmp_path: Path) -> None:
    classes, _ = _classes(tmp_path, {"Plain.groovy": """package com.acme
class Plain {
    String name = "x"
    void m() { other() }
    void other() { }
}
"""}, "Plain.groovy")
    assert classes["Plain"] == []
