"""RazorParser — the language parser owning ``.cshtml`` / ``.razor`` (was skipped).

Razor is HTML + embedded C# via ``@`` syntax. Unlike ``.aspx`` (no grammar → shadow-source),
the ``razor`` grammar (in ``tree_sitter_language_pack`` — no new dependency) parses markup and
the embedded C# in a single pass, so the template constructs are captured from real grammar
nodes (see :mod:`.statements`).

``.cshtml`` is an MVC / Razor-Pages **view** (``uiRole="template"``); ``.razor`` is a Blazor
**component** (``uiRole="component"``, like a Vue SFC). ``language`` is ``html`` — a Razor file
is an HTML dialect (the same reasoning as ``.aspx``); ``framework`` is ``razor``.

The ``@code { }`` block's C# methods (Blazor component logic) are a later step — the analogue
of Vue's ``<script>`` extraction — and are not captured here yet.
"""

from __future__ import annotations

from ...emit import file_id
from ...schemas import SCHEMA_VERSION, FileRecord
from ...utils import count_loc
from ..base import BaseParser, ParseContext
from ..treesitter import parse_source
from .mappings import FRAMEWORKS, STATEMENT_TYPES
from .statements import collect_razor_statements


class RazorParser(BaseParser):
    name = "razor"
    extensions: tuple[str, ...] = (".cshtml", ".razor")
    schema_version = SCHEMA_VERSION
    statement_types = STATEMENT_TYPES
    frameworks = FRAMEWORKS

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        fid = file_id(ctx.path)
        root = parse_source("razor", ctx.source, ctx.parse_timeout_micros).root_node
        # .razor is a Blazor component; .cshtml is a view template.
        ui_role = "component" if ctx.path.endswith(".razor") else "template"
        record = FileRecord(
            id=fid,
            path=ctx.path,
            type="code",
            language="html",
            framework="razor",
            uiRole=ui_role,
            loc=count_loc(ctx.source.decode("utf-8", "replace")),
        )
        if ctx.capture_statements:
            record.statements.extend(
                collect_razor_statements(
                    root,
                    ctx.source,
                    ctx.path,
                    fid,
                    set(),
                    ctx.statement_text_limit,
                    emit_routes=not self.is_fixture_file(ctx.path),
                )
            )
        return record
