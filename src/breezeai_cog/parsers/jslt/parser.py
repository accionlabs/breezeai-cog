"""JsltParser — a standalone-file **language parser** owning ``.jslt``.

JSLT (github.com/schibsted/jslt), a JSON query/transform language, has no tree-sitter
grammar in this project's language pack, and the only public third-party grammar is an
unmaintained single-author repository with no PyPI package or verified Python bindings — not
safe to build on. Structure is instead extracted by a hand-rolled line-scan (see
:mod:`.statements`) over JSLT's small, line-oriented top-level surface — ``import``/``def``/
``let`` declarations plus the trailing module (output) expression.

Unlike every tree-sitter-backed parser in this project, there is no grammar tree to run the
shared ``comments_common.py`` pass over, so ``//`` comments are not captured as their own
statements.

Statement capture is on by default (gated behind ``ctx.capture_statements``; the CLI's
``--no-capture-statements`` turns it off), same as every other parser; ``def`` functions are
structure and are captured either way.
"""

from __future__ import annotations

from ...emit import file_id
from ...schemas import SCHEMA_VERSION, FileRecord
from ...utils import count_loc
from ..base import BaseParser, ParseContext
from .mappings import FRAMEWORKS, STATEMENT_TYPES
from .statements import collect_jslt_declarations


class JsltParser(BaseParser):
    name = "jslt"
    extensions: tuple[str, ...] = (".jslt",)
    schema_version = SCHEMA_VERSION
    statement_types = STATEMENT_TYPES
    frameworks = FRAMEWORKS

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        source, path = ctx.source, ctx.path
        # utf-8-sig strips a leading BOM if present (a no-op otherwise) — without it, a
        # BOM lands on line 1 as a non-declaration column-0 character and the boundary
        # scan reads "declarations are over" immediately, collapsing the whole file into
        # one module_expression.
        text = source.decode("utf-8-sig", "replace")
        fid = file_id(path)
        seen_ids: set[str] = set()

        # No is_fixture_file gate: that check exists only for route/entry-point emitters
        # (base.py), and JSLT emits no routes — a fixture .jslt still has real declarations
        # worth capturing. Functions are structure, so they are captured even with
        # `--no-capture-statements`, like every other language's.
        statements, functions = collect_jslt_declarations(
            text, path, seen_ids, capture_statements=ctx.capture_statements
        )

        return FileRecord(
            id=fid,
            path=path,
            type="code",
            language="jslt",
            loc=count_loc(text),
            statements=statements,
            functions=functions,
            framework="jslt" if (statements or functions) else None,
        )
