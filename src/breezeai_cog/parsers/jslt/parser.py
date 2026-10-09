"""JsltParser — a standalone-file **language parser** owning ``.jslt``.

JSLT (github.com/schibsted/jslt), a JSON query/transform language, has no tree-sitter
grammar in this project's language pack. The plan's Decisions table records the rejected
alternative (adopting the only public third-party grammar, an unmaintained single-author
repository) and why: it fails this project's Vet Before Reuse bar. Structure is instead
extracted by a hand-rolled line-scan (see :mod:`.statements`) over JSLT's small,
line-oriented top-level surface — ``import``/``def``/``let`` declarations plus the trailing
module (output) expression.

Unlike every tree-sitter-backed parser in this project, there is no grammar tree to run the
shared ``comments_common.py`` pass over, so ``//`` comments are not captured as their own
statements in this v1 (out of scope — see the plan).

Semantic capture is gated behind ``--capture-statements``, same as every other parser.
"""

from __future__ import annotations

from ...emit import file_id
from ...schemas import SCHEMA_VERSION, FileRecord
from ...utils import count_loc
from ..base import BaseParser, ParseContext
from .mappings import FRAMEWORKS, STATEMENT_TYPES
from .statements import collect_jslt_statements


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

        statements = (
            collect_jslt_statements(text, path, seen_ids)
            if ctx.capture_statements and not self.is_fixture_file(path)
            else []
        )

        return FileRecord(
            id=fid,
            path=path,
            type="code",
            language="jslt",
            loc=count_loc(text),
            statements=statements,
            framework="jslt" if statements else None,
        )
