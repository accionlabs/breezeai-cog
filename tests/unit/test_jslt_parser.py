"""Standalone JSLT (JSON transform language) capture.

The ``jslt`` language parser owns ``.jslt`` files. Since JSLT has no tree-sitter grammar in
this project's language pack, structure comes from a hand-rolled line-scan: ``import``/
``let`` declarations and a trailing ``module_expression`` are flat Statements; top-level
(column-0) ``def`` declarations are ``Function`` records (independently searchable,
call-linked) with untyped ``Parameter``s and same-file ``calls[]`` resolution. Capture is on
by default; unlike every tree-sitter-backed parser in this project, ``//`` comments are not
captured as their own statements (no grammar tree to run the shared comment pass over).
"""

from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from breezeai_cog.core import registry
from breezeai_cog.emit import to_line
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.jslt.parser import JsltParser
from breezeai_cog.schemas import FileRecord

SOURCE = """\
// Transform a user record for the public API
import "common.jslt" as common

def full-name(p)
  p.firstName + " " + p.lastName

let greeting = "Hello"

{
  "name": full-name(.),
  "greeting": greeting,
  "note": "uses // not a comment"
}
"""


def _parse(
    tmp_path, filename: str, src: str, *, capture: bool = True, limit: int = 1000
) -> FileRecord:
    p = tmp_path / filename
    p.write_text(src)
    ctx = ParseContext(
        path=filename,
        abs_path=p,
        source=src.encode(),
        repo_root=tmp_path,
        capture_statements=capture,
        statement_text_limit=limit,
    )
    return JsltParser().parse_file(ctx)


def _kind(rec, s) -> str:
    """What a statement records — read from its `text` and parent, since every JSLT
    statement's `nodeType` is `synthetic` (no tree-sitter grammar)."""
    assert s.nodeType == "synthetic"
    if s.parentId != rec.id:
        return "function_body"
    if s.text.startswith("import"):
        return "import"
    if s.text.startswith("let"):
        return "let_binding"
    return "module_expression"


def _by_name(rec, name: str, kind: str):
    return next(s for s in rec.statements if s.name == name and _kind(rec, s) == kind)


def _func(rec, name: str):
    return next(f for f in rec.functions if f.name == name)


def _body(rec, fn) -> str:
    """The full-text `function_body` statement companion to `fn` (Function has no `text`
    field)."""
    return next(
        s for s in rec.statements if _kind(rec, s) == "function_body" and s.parentId == fn.id
    ).text


def test_language_and_extensions() -> None:
    p = JsltParser()
    assert p.name == "jslt"
    assert p.extensions == (".jslt",)
    assert p.frameworks == []  # JSLT is a language, with no framework on top


def test_import_is_captured_with_alias_as_name(tmp_path) -> None:
    rec = _parse(tmp_path, "transform.jslt", SOURCE)
    assert rec.language == "jslt"
    assert rec.framework is None
    imp = _by_name(rec, "common", "import")
    assert imp.parentId == rec.id
    assert imp.text == 'import "common.jslt" as common'


def test_def_is_a_function_record_with_untyped_param(tmp_path) -> None:
    rec = _parse(tmp_path, "transform.jslt", SOURCE)
    fn = _func(rec, "full-name")
    assert fn.parentId == rec.id
    assert [p.name for p in fn.params] == ["p"]
    assert fn.params[0].type == ""  # JSLT is untyped
    body = _body(rec, fn)
    assert body.startswith("def full-name(p)")
    assert 'p.firstName + " " + p.lastName' in body


def test_calls_resolve_to_same_file_def(tmp_path) -> None:
    src = "def helper(x)\n  x + 1\n\ndef main(y)\n  helper(y)\n\nmain(1)\n"
    rec = _parse(tmp_path, "transform.jslt", src)
    main_fn = _func(rec, "main")
    assert [c.name for c in main_fn.calls] == ["helper"]
    assert main_fn.calls[0].path == "transform.jslt"
    helper_fn = _func(rec, "helper")
    assert helper_fn.calls == []  # no calls in its own body


def test_recursive_call_resolves_to_self(tmp_path) -> None:
    src = "def fact(n)\n  if ($n <= 1) 1 else $n * fact($n - 1)\n\nfact(5)\n"
    rec = _parse(tmp_path, "transform.jslt", src)
    fn = _func(rec, "fact")
    assert [c.name for c in fn.calls] == ["fact"]
    assert fn.calls[0].path == "transform.jslt"


def test_call_to_parameter_not_resolved(tmp_path) -> None:
    # `f` is a parameter (a function-valued argument), not a top-level `def` -- calling it
    # must not produce a fabricated same-file edge.
    src = "def apply(f, x)\n  f(x)\n\napply(.,.)\n"
    rec = _parse(tmp_path, "transform.jslt", src)
    fn = _func(rec, "apply")
    assert fn.calls == []


def test_call_into_imported_module_not_linked_to_same_named_local_def(tmp_path) -> None:
    # `m:foo()` calls `foo` in the module imported `as m`; linking it to this file's own
    # `foo` would invent a self-recursion edge.
    src = 'import "other.jslt" as m\n\ndef foo()\n  m:foo()\n\nfoo()\n'
    rec = _parse(tmp_path, "transform.jslt", src)
    assert _func(rec, "foo").calls == []


def test_bare_call_after_operator_or_bracket_still_resolves(tmp_path) -> None:
    # The module-prefix guard must not drop real bare calls written right after an operator,
    # a bracket or a comma.
    src = "def one()\n  1\n\ndef sum(x)\n  [one(),(one())] + -one()+one()\n\nsum(1)\n"
    rec = _parse(tmp_path, "transform.jslt", src)
    assert [c.name for c in _func(rec, "sum").calls] == ["one"]


def test_name_in_comment_or_string_not_read_as_call(tmp_path) -> None:
    # A def's name written in a `//` comment or inside a string literal is not a call.
    src = (
        "def helper(x)\n  $x + 1\n\n"
        "def main(x)\n"
        "  // TODO: replace helper(x) later\n"
        '  {"label": "call helper(x) here", "esc": "a \\" helper(x)", "v": $x}\n\n'
        "main(.)\n"
    )
    rec = _parse(tmp_path, "transform.jslt", src)
    assert _func(rec, "main").calls == []


def test_call_after_string_and_comment_on_same_line_still_resolves(tmp_path) -> None:
    # Blanking a string or a trailing comment must not hide a real call beside it.
    src = 'def one()\n  1\n\ndef main(x)\n  "(" + one() // one() again\n\nmain(.)\n'
    rec = _parse(tmp_path, "transform.jslt", src)
    assert [c.name for c in _func(rec, "main").calls] == ["one"]


def test_call_in_own_header_not_mistaken_for_self_call(tmp_path) -> None:
    # The header line `def f(x)` must not read its own name (`f(`) as a call to itself.
    src = "def f(x)\n  x + 1\n\nf(1)\n"
    rec = _parse(tmp_path, "transform.jslt", src)
    fn = _func(rec, "f")
    assert fn.calls == []


def test_text_not_truncated_by_statement_text_limit(tmp_path) -> None:
    # Regression: this parser used to slice `[:limit]` itself, which fed the emit-time
    # lossless splitter (emit/split.py) pre-cut text and silently lost the tail. Truncation
    # now happens only at emit time -- the parser must capture the full def body regardless
    # of how small `limit` is.
    body_lines = "\n".join(f"    .field{i}" for i in range(50))
    src = f"def big(x)\n  x +\n{body_lines}\n\nbig(.)\n"
    rec = _parse(tmp_path, "transform.jslt", src, limit=20)
    body = _body(rec, _func(rec, "big"))
    assert ".field0" in body
    assert ".field49" in body
    assert len(body) > 20


def test_text_not_emptied_when_statement_text_limit_is_zero(tmp_path) -> None:
    # `statement_text_limit=0` is documented as "0 disables" -- it must not mean "empty
    # every statement's text", which is what the old `text[:0]` slicing did.
    rec = _parse(tmp_path, "transform.jslt", SOURCE, limit=0)
    binding = _by_name(rec, "greeting", "let_binding")
    assert binding.text != ""
    assert binding.text == 'let greeting = "Hello"'


def test_let_is_captured(tmp_path) -> None:
    rec = _parse(tmp_path, "transform.jslt", SOURCE)
    binding = _by_name(rec, "greeting", "let_binding")
    assert binding.text == 'let greeting = "Hello"'


def test_trailing_module_expression_captured_as_one_statement(tmp_path) -> None:
    rec = _parse(tmp_path, "transform.jslt", SOURCE)
    modules = [s for s in rec.statements if _kind(rec, s) == "module_expression"]
    assert len(modules) == 1
    assert modules[0].name is None
    assert '"greeting": greeting' in modules[0].text


def test_double_slash_inside_string_literal_not_treated_as_comment(tmp_path) -> None:
    rec = _parse(tmp_path, "transform.jslt", SOURCE)
    module = next(s for s in rec.statements if _kind(rec, s) == "module_expression")
    assert '"note": "uses // not a comment"' in module.text


def test_leading_comment_line_not_captured_as_a_statement(tmp_path) -> None:
    # No tree-sitter grammar -> no shared comment pass; `//` lines are simply not statements.
    rec = _parse(tmp_path, "transform.jslt", SOURCE)
    assert not any("Transform a user record" in s.text for s in rec.statements)
    assert len(rec.statements) == 4  # import + let + function_body + module_expression
    assert len(rec.functions) == 1  # full-name


def test_bare_expression_with_no_declarations(tmp_path) -> None:
    rec = _parse(tmp_path, "identity.jslt", ".\n")
    assert len(rec.statements) == 1
    assert _kind(rec, rec.statements[0]) == "module_expression"
    assert rec.statements[0].text == "."
    assert rec.functions == []


def test_nested_let_inside_def_stays_on_def_text_not_split(tmp_path) -> None:
    src = (
        "def full-name(p)\n"
        "  let first = p.firstName\n"
        "  let last = p.lastName\n"
        '  first + " " + last\n'
        "\n"
        "full-name(.)\n"
    )
    rec = _parse(tmp_path, "transform.jslt", src)
    let_bindings = [s for s in rec.statements if _kind(rec, s) == "let_binding"]
    assert let_bindings == []  # the nested lets are indented -- part of the def's body only
    body = _body(rec, _func(rec, "full-name"))
    assert "let first = p.firstName" in body
    assert "let last = p.lastName" in body


def test_let_value_starting_on_next_line_not_cut_off(tmp_path) -> None:
    # Regression: a `let`'s header ending with nothing after `=` (the value starts on the
    # next line) used to be read as a complete, valueless declaration, and the value itself
    # was then misread as a new boundary -- corrupting the real module expression that
    # followed it.
    src = 'let cfg =\n{\n  "a": 1\n}\n\n{"out": cfg}\n'
    rec = _parse(tmp_path, "transform.jslt", src)
    binding = _by_name(rec, "cfg", "let_binding")
    assert binding.text == 'let cfg =\n{\n  "a": 1\n}'
    module = next(s for s in rec.statements if _kind(rec, s) == "module_expression")
    assert module.text == '{"out": cfg}'


def test_def_body_starting_on_next_line_not_cut_off(tmp_path) -> None:
    # Same bug, for a `def` whose header ends with nothing after the closing `)` -- the body
    # itself at column 0 (not indented), so bracket-depth tracking alone (which already
    # handles an *indented* continuation) can't save it; only the incomplete-header check can.
    src = 'def f(x)\nif ($x) 1 else 2\n\n{"y": f(.a)}\n'
    rec = _parse(tmp_path, "transform.jslt", src)
    body = _body(rec, _func(rec, "f"))
    assert body == "def f(x)\nif ($x) 1 else 2"
    module = next(s for s in rec.statements if _kind(rec, s) == "module_expression")
    assert module.text == '{"y": f(.a)}'


def test_multiline_value_closing_brace_at_column_zero_not_a_new_boundary(tmp_path) -> None:
    # Regression: a `let`'s JSON-style object literal closing `}` flush at column 0 is
    # ordinary JSLT formatting, not a new top-level construct -- bracket-depth tracking must
    # keep it part of the `let`'s own span, not truncate it and corrupt the module expression
    # that follows.
    src = 'let config = {\n  "a": 1,\n  "b": 2\n}\n\n{ "result": config }\n'
    rec = _parse(tmp_path, "transform.jslt", src)
    binding = _by_name(rec, "config", "let_binding")
    assert binding.text == 'let config = {\n  "a": 1,\n  "b": 2\n}'
    module = next(s for s in rec.statements if _kind(rec, s) == "module_expression")
    assert module.text == '{ "result": config }'


def test_escaped_quote_before_comment_marker_not_treated_as_comment(tmp_path) -> None:
    # The `\"`-escape branch in `_strip_line_comment`: an escaped quote inside a string
    # must not toggle "in a string" early, or a later `//` in the same string would be
    # mis-read as a real comment marker and truncate the captured declaration.
    src = 'let x = "quote: \\" then // not a comment"\n\nx\n'
    rec = _parse(tmp_path, "t.jslt", src)
    binding = _by_name(rec, "x", "let_binding")
    assert binding.text == 'let x = "quote: \\" then // not a comment"'


def test_escaped_quote_inside_string_not_miscounted_as_bracket(tmp_path) -> None:
    # The same `\"`-escape branch in `_bracket_delta`: if it were missing, the escaped quote
    # would prematurely end the "in a string" state, letting the subsequent literal `{`
    # (still inside the JSLT string value) be miscounted as a real, unclosed bracket -- which
    # would push every later line's nesting depth to 1 forever, so the trailing module
    # expression would never be recognized as a boundary at all.
    src = 'let cfg = "esc \\" opens { a brace"\n\ncfg\n'
    rec = _parse(tmp_path, "t.jslt", src)
    modules = [s for s in rec.statements if _kind(rec, s) == "module_expression"]
    assert len(modules) == 1
    assert modules[0].text == "cfg"


def test_utf8_bom_stripped_before_scanning(tmp_path) -> None:
    # Regression: decoding with plain "utf-8" leaves a leading BOM on line 1 as a
    # non-declaration column-0 character, which `_split_declarations` reads as
    # "declarations are over" immediately -- collapsing the whole file into a single
    # module_expression. "utf-8-sig" strips it before the scanner ever sees it.
    text = 'import "common.jslt" as common\n\nlet x = 1\n\ndef f(a) a\n\n{"r": f(x)}\n'
    p = tmp_path / "bom.jslt"
    src_bytes = text.encode("utf-8-sig")
    p.write_bytes(src_bytes)
    ctx = ParseContext(
        path="bom.jslt",
        abs_path=p,
        source=src_bytes,
        repo_root=tmp_path,
        capture_statements=True,
        statement_text_limit=1000,
    )
    rec = JsltParser().parse_file(ctx)
    kinds = [_kind(rec, s) for s in rec.statements]
    assert kinds == ["import", "let_binding", "module_expression", "function_body"]
    assert [f.name for f in rec.functions] == ["f"]


def test_capture_gate_drops_statements_but_keeps_functions(tmp_path) -> None:
    # Functions are structure (every language keeps them with --no-capture-statements);
    # only the statements are gated.
    on = _parse(tmp_path, "transform.jslt", SOURCE)
    off = _parse(tmp_path, "transform.jslt", SOURCE, capture=False)
    assert off.statements == []
    assert off.language == "jslt"
    assert [f.model_dump() for f in off.functions] == [f.model_dump() for f in on.functions]


def test_fixture_file_still_captures_declarations(tmp_path) -> None:
    # JSLT emits no routes, so `is_fixture_file` (route/entry-point-only, per base.py) must
    # not gate its capture at all -- statements and functions alike.
    rec = _parse(tmp_path, "transform.test.jslt", SOURCE)
    assert any(_kind(rec, s) == "let_binding" for s in rec.statements)
    assert any(f.name == "full-name" for f in rec.functions)


def test_empty_file_yields_no_statements(tmp_path) -> None:
    rec = _parse(tmp_path, "empty.jslt", "")
    assert rec.statements == []
    assert rec.functions == []


def test_every_statement_is_synthetic_and_declared(tmp_path) -> None:
    # No tree-sitter grammar means no backing AST node, so every statement is `synthetic`.
    rec = _parse(tmp_path, "transform.jslt", SOURCE)
    assert {s.nodeType for s in rec.statements} == {"synthetic"}
    assert JsltParser().statement_types == ["synthetic"]


def test_records_validate_against_schema(tmp_path) -> None:
    rec = _parse(tmp_path, "transform.jslt", SOURCE)
    validator = Draft202012Validator(FileRecord.model_json_schema(by_alias=True))
    errors = list(validator.iter_errors(json.loads(to_line(rec))))
    assert not errors, errors
    assert rec.statements  # non-empty


def test_registered_and_selected_by_extension() -> None:
    registry.clear()
    registry.discover_builtin()
    sel = registry.select("transform.jslt", SOURCE.encode())
    assert sel is not None and sel.name == "jslt"
    assert ".jslt" in registry.capabilities()["extensions"]
    assert "jslt" in registry.capabilities()["languages"]
    registry.clear()
