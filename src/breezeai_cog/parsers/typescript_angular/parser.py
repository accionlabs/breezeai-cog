"""AngularParser — a TypeScript framework parser selected (one parser per file) when
``claims`` finds an ``@angular/`` import. Full TS extraction + Angular route configs.
Coexists with NestJS because selection is per-file by ``claims``."""

from __future__ import annotations

from ...schemas import FileRecord
from ..base import ParseContext
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
        # this runs regardless of --capture-statements).
        mark_angular_ui_roles(record)
        # framework axis: every file this parser claims IS an Angular file — stamp it
        # unconditionally so "list Angular files" is answerable, not only on route files. The
        # JS/TS distinction lives on the orthogonal `language` axis, set by the base extractor.
        record.framework = "angular"
        if ctx.capture_statements and not self.is_fixture_file(ctx.path):  # gated by --capture-statements; skip fixtures (R4)
            routes = detect_angular_routes(
                root, ctx.source, ctx.path, seen_ids={s.id for s in record.statements},
                index=ctx.resolution_index,
            )
            if routes:
                record.statements.extend(routes)  # framework already stamped above
        return record
