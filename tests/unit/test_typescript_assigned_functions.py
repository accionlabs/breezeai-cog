"""A function assigned where callers find it by name (anonymous-functions Rule 2a, JS/TS, the
names no Function had before): ``exports.x`` / ``module.exports.x`` → ``x``,
``module.exports`` → ``default``, ``X.prototype.m`` → ``m`` with ``receiverType`` X. Never an
ambiguous name (Rule 2e). The assignment statement that only binds it is not repeated."""

from __future__ import annotations

from pathlib import Path

from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.typescript.parser import TypeScriptParser


def _parse(tmp_path: Path, src: str):
    p = tmp_path / "app.js"
    p.write_text(src)
    ctx = ParseContext(path="app.js", abs_path=p, source=p.read_bytes(), repo_root=tmp_path,
                       capture_statements=True)
    return TypeScriptParser().parse_file(ctx)


def _fns(rec):
    return {f.name: (f.alias, f.receiverType, [(c.name, c.path) for c in f.calls])
            for f in rec.functions}


def test_exports_and_prototype_assignments_name_their_function(tmp_path: Path) -> None:
    rec = _parse(tmp_path, """exports.load = function loadAll() { fetchAll(); loadAll(); };
module.exports.save = (x) => put(x);
User.prototype.greet = function () { hello(); };
""")
    assert _fns(rec) == {
        "load": ("loadAll", None, [("fetchAll", None), ("load", "app.js")]),
        "save": (None, None, [("put", None)]),
        "greet": (None, "User", [("hello", None)]),
    }
    # the statements that only bind them are not repeated; the file has no code of its own
    assert [s for s in rec.statements if s.parentId == rec.id] == [] and rec.calls is None


def test_module_exports_of_a_function_is_the_default(tmp_path: Path) -> None:
    rec = _parse(tmp_path, "module.exports = function enableAuthentication(s) { s.x(); };\n")
    assert _fns(rec) == {"default": ("enableAuthentication", None, [("x", None)])}


def test_an_ambiguous_name_names_no_function(tmp_path: Path) -> None:
    rec = _parse(tmp_path, """exports.a = exports.b = function () { both(); };
exports.x = () => one();
exports.x = () => two();
""")
    assert rec.functions == []
    assert [c.name for c in rec.calls] == ["both", "one", "two"]  # the file's code, as before
