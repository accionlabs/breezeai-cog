"""ReactParser — a TypeScript framework parser. Selected over the base TypeScriptParser
(single parser per file) when ``claims`` finds a ``react-router`` import; reuses
``TypeScriptParser.extract`` on the shared tree, then adds React Router routes. It
coexists with the other TS framework parsers (NestJS, Angular, LoopBack) because
selection is per-file by ``claims`` (ARCHITECTURE.md §4)."""

from __future__ import annotations

from ...schemas import FileRecord
from ..base import ParseContext
from ..treesitter import parse_source
from ..typescript.parser import TypeScriptParser
from .routes import detect_react_routes


class ReactParser(TypeScriptParser):
    name = "typescript-react"
    priority = 10
    frameworks = ["react"]

    def claims(self, path: str, source: bytes) -> bool:
        return b"react-router" in source

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        grammar = "tsx" if ctx.path.endswith((".tsx", ".jsx")) else "typescript"
        root = parse_source(grammar, ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)  # inherited base extraction (one parse)
        if ctx.capture_statements and not self.is_fixture_file(ctx.path):  # gated (spec A4); skip fixtures (R4)
            routes = detect_react_routes(
                root, ctx.source, ctx.path, seen_ids={s.id for s in record.statements}
            )
            if routes:
                record.statements.extend(routes)
                record.framework = "react"
        return record
