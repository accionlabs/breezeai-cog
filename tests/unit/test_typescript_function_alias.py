"""``Function.alias`` (JS/TS): a named function expression stored under another name keeps its
own name, and a call to that name inside its own body is a call to the function itself."""

from __future__ import annotations

from pathlib import Path

import pytest

from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.typescript.parser import TypeScriptParser


def _functions(tmp_path: Path, src: str, name: str = "app.js") -> dict[str, tuple]:
    p = tmp_path / name
    p.write_text(src)
    ctx = ParseContext(path=name, abs_path=p, source=p.read_bytes(), repo_root=tmp_path,
                       capture_statements=True)
    rec = TypeScriptParser().parse_file(ctx)
    return {f.name: (f.alias, [(c.name, c.path) for c in f.calls]) for f in rec.functions}


@pytest.mark.parametrize("name", ["app.js", "app.ts"])
def test_own_name_is_the_alias_and_its_self_call_the_function(tmp_path: Path, name: str) -> None:
    fns = _functions(tmp_path, """const fact = function factorial(n) { return n ? n * factorial(n - 1) : 1; };
function factorial() { return 0; }
""", name)
    # never the unrelated `factorial` of the same file
    assert fns["fact"] == ("factorial", [("fact", name)])
    assert fns["factorial"] == (None, [])


def test_object_property_and_nested_function(tmp_path: Path) -> None:
    fns = _functions(tmp_path, """const api = { list: function listItems() { get(); listItems(); } };
function outer() { const inner = function own() { own(); }; }
""")
    assert fns["api.list"] == ("listItems", [("get", None), ("api.list", "app.js")])
    assert fns["inner"] == ("own", [("inner", "app.js")])


def test_no_alias_when_there_is_one_name(tmp_path: Path) -> None:
    fns = _functions(tmp_path, """const api = { x: function x() {} };
const f = () => g();
function h() { h(); }
function w() { arr.forEach(function step() {}); }
""")
    assert fns["api.x"][0] is None  # the same name twice
    assert fns["f"][0] is None  # an arrow function has no name of its own
    assert fns["h"][0] is None
    assert fns["step"][0] is None  # a named callback: one name only


def test_a_parameter_or_local_of_that_name_hides_it(tmp_path: Path) -> None:
    fns = _functions(tmp_path, """const f = function g(g) { g(); };
const k = function m() { const m = load(); m(); };
""")
    assert fns["f"] == ("g", [("g", None)])
    assert fns["k"] == ("m", [("load", None), ("m", None)])
