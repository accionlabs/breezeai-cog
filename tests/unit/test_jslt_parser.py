"""Standalone JSLT (JSON transform language) capture.

The ``jslt`` language parser owns ``.jslt`` files. Since JSLT has no tree-sitter grammar in
this project's language pack (see the plan's Decisions table for why the only public
third-party grammar was rejected), structure comes from a hand-rolled line-scan: top-level
(column-0) ``import``/``def``/``let`` declarations, plus a trailing ``module_expression``
for the output expression. Capture is gated on ``--capture-statements``; unlike every
tree-sitter-backed parser in this project, ``//`` comments are not captured as their own
statements (no grammar tree to run the shared comment pass over).
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


def _parse(tmp_path, filename: str, src: str, *, capture: bool = True) -> FileRecord:
    p = tmp_path / filename
    p.write_text(src)
    ctx = ParseContext(
        path=filename,
        abs_path=p,
        source=src.encode(),
        repo_root=tmp_path,
        capture_statements=capture,
        statement_text_limit=1000,
    )
    return JsltParser().parse_file(ctx)


def _by_name(rec, name: str, node_type: str):
    return next(s for s in rec.statements if s.name == name and s.nodeType == node_type)


def test_language_and_extensions() -> None:
    p = JsltParser()
    assert p.name == "jslt"
    assert p.extensions == (".jslt",)
    assert "jslt" in p.frameworks


def test_import_is_captured_with_alias_as_name(tmp_path) -> None:
    rec = _parse(tmp_path, "transform.jslt", SOURCE)
    assert rec.language == "jslt"
    assert rec.framework == "jslt"
    imp = _by_name(rec, "common", "import")
    assert imp.parentId == rec.id
    assert imp.text == 'import "common.jslt" as common'


def test_def_captures_full_multiline_body(tmp_path) -> None:
    rec = _parse(tmp_path, "transform.jslt", SOURCE)
    fn = _by_name(rec, "full-name", "function_def")
    assert "def full-name(p)" in fn.text
    assert 'p.firstName + " " + p.lastName' in fn.text


def test_let_is_captured(tmp_path) -> None:
    rec = _parse(tmp_path, "transform.jslt", SOURCE)
    binding = _by_name(rec, "greeting", "let_binding")
    assert binding.text == 'let greeting = "Hello"'


def test_trailing_module_expression_captured_as_one_statement(tmp_path) -> None:
    rec = _parse(tmp_path, "transform.jslt", SOURCE)
    modules = [s for s in rec.statements if s.nodeType == "module_expression"]
    assert len(modules) == 1
    assert modules[0].name is None
    assert '"greeting": greeting' in modules[0].text


def test_double_slash_inside_string_literal_not_treated_as_comment(tmp_path) -> None:
    rec = _parse(tmp_path, "transform.jslt", SOURCE)
    module = next(s for s in rec.statements if s.nodeType == "module_expression")
    assert '"note": "uses // not a comment"' in module.text


def test_leading_comment_line_not_captured_as_a_statement(tmp_path) -> None:
    # No tree-sitter grammar -> no shared comment pass; `//` lines are simply not statements.
    rec = _parse(tmp_path, "transform.jslt", SOURCE)
    assert not any("Transform a user record" in s.text for s in rec.statements)
    assert len(rec.statements) == 4  # import + def + let + module_expression, nothing else


def test_bare_expression_with_no_declarations(tmp_path) -> None:
    rec = _parse(tmp_path, "identity.jslt", ".\n")
    assert len(rec.statements) == 1
    assert rec.statements[0].nodeType == "module_expression"
    assert rec.statements[0].text == "."


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
    let_bindings = [s for s in rec.statements if s.nodeType == "let_binding"]
    assert let_bindings == []  # the nested lets are indented -- part of the def's body only
    fn = _by_name(rec, "full-name", "function_def")
    assert "let first = p.firstName" in fn.text
    assert "let last = p.lastName" in fn.text


def test_capture_gate(tmp_path) -> None:
    rec = _parse(tmp_path, "transform.jslt", SOURCE, capture=False)
    assert rec.statements == []
    assert rec.language == "jslt"
    assert rec.framework is None


def test_empty_file_yields_no_statements(tmp_path) -> None:
    rec = _parse(tmp_path, "empty.jslt", "")
    assert rec.statements == []


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
