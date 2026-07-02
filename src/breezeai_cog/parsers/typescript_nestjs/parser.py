"""NestJSParser — a TypeScript framework parser. Selected over the base
TypeScriptParser (single parser per file) when ``claims`` finds a NestJS signature;
reuses ``TypeScriptParser.extract`` on the shared tree, then adds NestJS routes. It
coexists with other TS framework parsers (Angular) because selection is per-file by
``claims`` (ARCHITECTURE.md §4)."""

from __future__ import annotations

from ...schemas import FileRecord
from ..base import ParseContext
from ..treesitter import parse_source
from ..typescript.parser import TypeScriptParser
from .routes import detect_nest_routes


class NestJSParser(TypeScriptParser):
    name = "typescript-nestjs"
    # Above ExpressParser (priority 10): NestJS is built on Express and its controllers
    # routinely ``import { Request } from 'express'``, which the Express parser also
    # claims. A ``@nestjs/`` signature is decisive, so this must win the selection.
    priority = 20
    frameworks = ["nestjs"]

    def claims(self, path: str, source: bytes) -> bool:
        return b"@nestjs/" in source

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        grammar = "tsx" if ctx.path.endswith((".tsx", ".jsx")) else "typescript"
        root = parse_source(grammar, ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)  # inherited base extraction (one parse)
        if ctx.capture_statements:  # routes are statements — gated by --capture-statements (spec A4)
            routes = detect_nest_routes(
                root, ctx.source, ctx.path, seen_ids={s.id for s in record.statements}
            )
            if routes:
                record.statements.extend(routes)
                record.framework = "nestjs"
        return record
