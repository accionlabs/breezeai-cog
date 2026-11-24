"""AngularParser — a TypeScript framework parser selected (one parser per file) when
``claims`` finds an ``@angular/`` import. Full TS extraction + Angular route configs.
Coexists with NestJS because selection is per-file by ``claims``."""

from __future__ import annotations

from tree_sitter import Node

from ...schemas import FileRecord
from ..base import ParseContext
from ..emits import apply
from ..html.statements import collect_template_statements
from ..template_common.inline_templates import inline_template_markup
from ..template_common.markup import read_page_markup
from ..treesitter import parse_source
from ..typescript.parser import TypeScriptParser
from .components import mark_angular_ui_roles
from .routes import detect_angular_routes


class AngularParser(TypeScriptParser):
    name = "typescript-angular"
    priority = 10
    frameworks = ["angular"]

    def claims(self, path: str, source: bytes) -> bool:
        return b"@angular/" in source

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        grammar = "tsx" if ctx.path.endswith((".tsx", ".jsx")) else "typescript"
        root = parse_source(grammar, ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)  # full TS extraction (one parse)
        # @Component / @Directive / @Pipe classes -> uiRole (classes are always captured, so
        # this runs regardless of statement capture).
        mark_angular_ui_roles(record)
        # framework axis: every file this parser claims IS an Angular file — stamp it
        # unconditionally so "list Angular files" is answerable, not only on route files. The
        # JS/TS distinction lives on the orthogonal `language` axis, set by the base extractor.
        record.framework = "angular"
        if ctx.capture_statements and not self.is_fixture_file(ctx.path):  # gated by statement capture; skip fixtures (R4)
            routes = detect_angular_routes(root, ctx.source, ctx.path, index=ctx.resolution_index)
            apply(record, routes, ctx.source, ctx.path)  # framework already stamped above
        if ctx.capture_statements:
            self._inline_template(record, root, ctx)
        return record

    def _inline_template(self, record: FileRecord, root: Node, ctx: ParseContext) -> None:
        """A ``template: `…``` written in the component: read as its ``.html`` template would be
        (bindings, links and form posts), at its true lines in this file."""
        markup = inline_template_markup(root, ctx.source)
        if markup is None:
            return
        troot = parse_source("angular", markup, ctx.parse_timeout_micros).root_node
        record.statements.extend(
            collect_template_statements(
                troot,
                markup,
                ctx.path,
                parent_id=record.id,
                seen_ids={s.id for s in record.statements},
                limit=ctx.statement_text_limit,
                framework="angular",
                emit_routes=not self.is_fixture_file(ctx.path),
            )
        )
        read_page_markup(
            record, markup, ctx.path, ctx.parse_timeout_micros,
            links=not self.is_fixture_file(ctx.path), source=ctx.source,
        )
