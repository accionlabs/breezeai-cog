"""LoopBackParser — a TypeScript framework parser. Selected over the base
TypeScriptParser (single parser per file) when ``claims`` finds a LoopBack signature;
reuses ``TypeScriptParser.extract`` on the shared tree, then adds LoopBack routes. It
coexists with other TS framework parsers (NestJS, Angular) because selection is per-file
by ``claims``."""

from __future__ import annotations

from ...schemas import FileRecord
from ..base import ParseContext
from ..emits import apply
from ..treesitter import parse_source
from ..typescript.parser import TypeScriptParser
from .routes import detect_loopback_routes


class LoopBackParser(TypeScriptParser):
    name = "typescript-loopback"
    # Above the other TypeScript framework parsers (priority 10): a ``@loopback/`` signature
    # is decisive, so this must win the selection. LoopBack is built on Express; Express
    # routes in the same file still come from the additive ``express`` detector.
    priority = 20
    frameworks = ["loopback"]

    def claims(self, path: str, source: bytes) -> bool:
        return b"@loopback/" in source

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        grammar = "tsx" if ctx.path.endswith((".tsx", ".jsx")) else "typescript"
        root = parse_source(grammar, ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)  # inherited base extraction (one parse)
        if ctx.capture_statements and not self.is_fixture_file(ctx.path):  # gated by statement capture; skip fixtures (R4)
            routes = detect_loopback_routes(root, ctx.source, ctx.path, index=ctx.resolution_index)
            if routes:
                apply(record, routes, ctx.source, ctx.path)
                record.framework = "loopback"
        return record
