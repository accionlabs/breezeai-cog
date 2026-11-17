"""A call on a variable resolves with the type that variable has in the calling method: a
same-named parameter of another method, or a field a local hides, does not lend its type
(Java and Groovy)."""

from __future__ import annotations

from pathlib import Path

import pytest

from breezeai_cog.parsers.base import BaseParser, ParseContext
from breezeai_cog.parsers.groovy.parser import GroovyParser
from breezeai_cog.parsers.java.parser import JavaParser

_LANGS = {
    "java": (JavaParser, "src/main/java/com/acme", "java", ";"),
    "groovy": (GroovyParser, "src/main/groovy/com/acme", "groovy", ""),
}
_TYPES = {
    "Order": "public void ship() { }",
    "Customer": "public void ship() { }",
    "Repo": "public void save() { }",
    "Audit": "public void save() { }",
}


def _calls(tmp_path: Path, lang: str, service: str) -> dict[str, dict[str, str | None]]:
    parser_cls, root, ext, semi = _LANGS[lang]
    for name, body in _TYPES.items():
        p = tmp_path / root / f"{name}.{ext}"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"package com.acme{semi}\npublic class {name} {{ {body} }}\n")
    rel = f"{root}/Service.{ext}"
    (tmp_path / rel).write_text(f"package com.acme{semi}\n" + service.replace(";", semi))
    parser: BaseParser = parser_cls()
    index = parser.build_index(tmp_path, list(tmp_path.rglob(f"*.{ext}")))
    path = tmp_path / rel
    record = parser.parse_file(ParseContext(path=rel, abs_path=path, source=path.read_bytes(),
                                            repo_root=tmp_path, resolution_index=index))
    out: dict[str, dict[str, str | None]] = {}
    for f in record.functions:
        out[f.name] = {c.name: c.path.rsplit("/", 1)[-1] if c.path else None for c in f.calls}
    return out


@pytest.mark.parametrize("lang", sorted(_LANGS))
def test_a_parameter_has_its_own_method_type(tmp_path: Path, lang: str) -> None:
    calls = _calls(tmp_path, lang, """public class Service {
    public void first(Order x) { x.ship(); }
    public void second(Customer x) { x.ship(); }
}
""")
    ext = _LANGS[lang][2]
    assert calls == {"first": {"ship": f"Order.{ext}"}, "second": {"ship": f"Customer.{ext}"}}


@pytest.mark.parametrize("lang", sorted(_LANGS))
def test_a_local_hiding_a_field_leaves_the_call_empty(tmp_path: Path, lang: str) -> None:
    # `repo` is the field before the local's declaration and the local after it, so its type
    # inside third() is not one; `this.repo` is always the field.
    calls = _calls(tmp_path, lang, """public class Service {
    private Repo repo;
    public void third() { Audit repo = new Audit(); repo.save(); }
    public void fourth() { repo.save(); }
    public void fifth() { Audit repo = new Audit(); this.repo.save(); }
}
""")
    ext = _LANGS[lang][2]
    assert calls == {"third": {"save": None}, "fourth": {"save": f"Repo.{ext}"},
                     "fifth": {"save": f"Repo.{ext}"}}


@pytest.mark.parametrize("lang", sorted(_LANGS))
def test_a_parameter_hides_a_field(tmp_path: Path, lang: str) -> None:
    calls = _calls(tmp_path, lang, """public class Service {
    private Repo repo;
    public void run(Audit repo) { repo.save(); }
}
""")
    assert calls == {"run": {"save": f"Audit.{_LANGS[lang][2]}"}}


@pytest.mark.parametrize("lang", sorted(_LANGS))
def test_a_name_declared_with_two_types_in_one_method_leaves_the_call_empty(
    tmp_path: Path, lang: str,
) -> None:
    calls = _calls(tmp_path, lang, """public class Service {
    public void run(boolean b) {
        if (b) { Order x = new Order(); x.ship(); }
        else { Customer x = new Customer(); x.ship(); }
    }
}
""")
    assert calls == {"run": {"ship": None}}


@pytest.mark.parametrize("lang", sorted(_LANGS))
def test_an_inner_class_sees_its_outer_class_fields(tmp_path: Path, lang: str) -> None:
    calls = _calls(tmp_path, lang, """public class Service {
    private Repo repo;
    private Order order;
    class Inner {
        private Customer order;
        public void run() { repo.save(); order.ship(); }
    }
}
""")
    ext = _LANGS[lang][2]
    assert calls == {"run": {"save": f"Repo.{ext}", "ship": f"Customer.{ext}"}}


@pytest.mark.parametrize("lang", sorted(_LANGS))
def test_a_name_the_method_does_not_declare_takes_the_file_s_one_type(
    tmp_path: Path, lang: str,
) -> None:
    # `repo` is a field Service inherits; the constructor parameter feeding it says its type.
    calls = _calls(tmp_path, lang, """public class Service extends Base {
    public Service(Repo repo) { super(repo); }
    public void run() { repo.save(); }
}
""")
    assert calls["run"] == {"save": f"Repo.{_LANGS[lang][2]}"}


@pytest.mark.parametrize("lang", sorted(_LANGS))
def test_a_name_the_file_declares_with_two_types_is_not_borrowed(tmp_path: Path, lang: str) -> None:
    calls = _calls(tmp_path, lang, """public class Service extends Base {
    public void a(Repo repo) { }
    public void b(Audit repo) { }
    public void run() { repo.save(); }
}
""")
    assert calls["run"] == {"save": None}


@pytest.mark.parametrize("lang", sorted(_LANGS))
def test_a_local_created_with_new_has_that_type(tmp_path: Path, lang: str) -> None:
    decl = "var" if lang == "java" else "def"
    calls = _calls(tmp_path, lang, f"""public class Service {{
    public void run() {{ {decl} a = new Audit(); a.save(); }}
    public void other(Repo a) {{ }}
}}
""")
    assert calls["run"] == {"save": f"Audit.{_LANGS[lang][2]}"}


def test_groovy_an_untyped_local_or_closure_parameter_hides_a_field(tmp_path: Path) -> None:
    calls = _calls(tmp_path, "groovy", """class Service {
    Repo repo
    void local() { def repo = new Audit(); repo.save() }
    void closure(List xs) { xs.each { repo -> repo.save() } }
    void typedClosure(List xs) { xs.each { Audit a -> a.save() } }
}
""")
    assert calls == {"local": {"save": None}, "closure": {"each": None, "save": None},
                     "typedClosure": {"each": None, "save": "Audit.groovy"}}


def test_java_this_in_an_anonymous_class_is_not_the_outer_field(tmp_path: Path) -> None:
    calls = _calls(tmp_path, "java", """public class Service {
    private Repo repo;
    public void run() {
        new Runnable() {
            private Audit repo;
            public void run() { this.repo.save(); }
        };
    }
}
""")
    assert calls == {"run": {"save": None}}


def test_java_a_record_component_is_a_field(tmp_path: Path) -> None:
    calls = _calls(tmp_path, "java", """public record Service(Repo repo) {
    public void run() { repo.save(); }
}
""")
    assert calls == {"run": {"save": "Repo.java"}}


def test_java_a_variable_hides_a_class_of_the_same_name(tmp_path: Path) -> None:
    calls = _calls(tmp_path, "java", """public class Service {
    public void run(Customer Order) { Order.ship(); }
}
""")
    assert calls == {"run": {"ship": "Customer.java"}}
