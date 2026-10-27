"""RazorParser — the language parser owning ``.cshtml`` / ``.razor`` (was skipped).

Razor is HTML + embedded C# via ``@`` syntax. Unlike ``.aspx`` (no grammar → shadow-source),
the ``razor`` grammar (in ``tree_sitter_language_pack`` — no new dependency) parses markup and
the embedded C# in a single pass, so the template constructs are captured from real grammar
nodes (see :mod:`.statements`).

``.cshtml`` is an MVC / Razor-Pages **view** (``uiRole="template"``); ``.razor`` is a Blazor
**component** (``uiRole="component"``, like a Vue SFC). ``language`` is ``html`` — a Razor file
is an HTML dialect (the same reasoning as ``.aspx``); ``framework`` is ``razor``.

The ``@code { }`` block's C# methods (Blazor component logic) are captured as Functions
parented to the component File — the analogue of Vue's ``<script>`` extraction. The razor
grammar embeds the C# grammar, so the methods are the same nodes the C# parser builds; we
reuse ``build_method`` directly (no shadow needed). This lets an ``@onclick="Remove"`` handler
resolve to the ``Remove`` method declared in the same file.
"""

from __future__ import annotations

from tree_sitter import Node

from ...emit import file_id
from ...schemas import SCHEMA_VERSION, FileRecord
from ...utils import count_loc
from ..base import BaseParser, HandlerLocator, ParseContext
from ..csharp.functions import build_method
from ..detection.jquery_calls import detect_inline_jquery
from ..treesitter import parse_source
from .mappings import FRAMEWORKS, STATEMENT_TYPES
from .page_models import page_model_locator
from .statements import collect_razor_statements

#: C# member declarations inside an ``@code { }`` block that become Functions. The razor
#: grammar embeds the C# grammar, so these are the same node types (and fields) the C# parser
#: uses — ``build_method`` works on them directly (no shadow needed).
_CODE_METHODS = ("method_declaration", "constructor_declaration", "local_function_statement")


class RazorParser(BaseParser):
    name = "razor"
    extensions: tuple[str, ...] = (".cshtml", ".razor")
    # Both are markup: .cshtml is a view, .razor a Blazor component. This parser owns
    # nothing else, so it goes idle entirely without --capture-templates.
    template_extensions: tuple[str, ...] = (".cshtml", ".razor")
    schema_version = SCHEMA_VERSION
    statement_types = STATEMENT_TYPES
    frameworks = FRAMEWORKS

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        fid = file_id(ctx.path)
        root = parse_source("razor", ctx.source, ctx.parse_timeout_micros).root_node
        # .razor is a Blazor component; .cshtml is a view template.
        ui_role = "component" if ctx.path.lower().endswith(".razor") else "template"
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
            seen_ids: set[str] = set()
            record.statements.extend(
                collect_razor_statements(
                    root,
                    ctx.source,
                    ctx.path,
                    fid,
                    seen_ids,
                    ctx.statement_text_limit,
                    emit_routes=not self.is_fixture_file(ctx.path),
                )
            )
            # @code { } methods → Functions (Blazor component logic), parented to the component
            # File, so an @onclick="Remove" handler resolves to the Remove method in this file.
            self._extract_code_methods(root, ctx, fid, seen_ids, record)
            # inline <script> jQuery AJAX calls → api_call (the markup grammar skips script bodies)
            detect_inline_jquery(record, ctx.source, ctx.path, ctx.parse_timeout_micros)
        return record

    def handler_locator(self, ctx: ParseContext) -> HandlerLocator | None:
        # A page's @model class, in its code-behind (:mod:`.page_models`). A .razor component
        # is its own handler, already located (line 1).
        if not ctx.path.lower().endswith(".cshtml"):
            return None
        return page_model_locator(ctx.abs_path, ctx.path, ctx.parse_timeout_micros)

    @staticmethod
    def _extract_code_methods(
        root: Node, ctx: ParseContext, fid: str, seen_ids: set[str], record: FileRecord
    ) -> None:
        """Extract each ``@code`` block's C# methods as Functions (parented to the File) plus
        their body statements, reusing the C# ``build_method``. Only the razor_block's direct
        member declarations are built — ``build_method`` handles nested local functions itself,
        so the whole-tree walk stays free of duplicates."""
        stack: list[Node] = [root]
        blocks: list[Node] = []
        while stack:
            node = stack.pop()
            if node.type == "razor_block":
                blocks.append(node)
            stack.extend(node.named_children)
        for block in blocks:
            for member in block.named_children:
                if member.type not in _CODE_METHODS:
                    continue
                fns, stmts = build_method(
                    member,
                    ctx.source,
                    ctx.path,
                    parent_id=fid,
                    class_name=None,
                    seen_ids=seen_ids,
                    capture=True,
                    limit=ctx.statement_text_limit,
                )
                record.functions.extend(fns)
                record.statements.extend(stmts)
