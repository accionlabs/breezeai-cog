"""LoopBackParser — a TypeScript framework parser. Selected over the base
TypeScriptParser (single parser per file) when ``claims`` finds a LoopBack signature;
reuses ``TypeScriptParser.extract`` on the shared tree, then adds LoopBack routes. It
coexists with other TS framework parsers (NestJS, Angular) because selection is per-file
by ``claims`` (ARCHITECTURE.md §4)."""

from __future__ import annotations

from ...schemas import FileRecord
from ..base import ParseContext
from ..treesitter import parse_source
from ..typescript.parser import TypeScriptParser
from .routes import detect_loopback_routes


class LoopBackParser(TypeScriptParser):
    name = "typescript-loopback"
    priority = 10
    frameworks = ["loopback"]

    def claims(self, path: str, source: bytes) -> bool:
        return b"@loopback/" in source

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        grammar = "tsx" if ctx.path.endswith((".tsx", ".jsx")) else "typescript"
        root = parse_source(grammar, ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)  # inherited base extraction (one parse)
        if ctx.capture_statements:  # routes are statements — gated by --capture-statements (spec A4)
            routes = detect_loopback_routes(
                root, ctx.source, ctx.path, seen_ids={s.id for s in record.statements}
            )
            if routes:
                record.statements.extend(routes)
                record.framework = "loopback"
        return record
