"""NestJSParser — a TypeScript framework parser that overrides the base
TypeScriptParser for the files it matches (single parse, reuses
``TypeScriptParser.extract`` on the shared tree, then adds NestJS routes).

Mirrors the FastAPI parser. ``overrides = ("typescript",)`` makes the registry skip
the base TS parser. (Only one override-framework parser can own a language; a second
TS framework that must coexist would compose instead — see ARCHITECTURE.md §4.)
"""

from __future__ import annotations

from ...schemas import FileRecord
from ..base import ParseContext
from ..treesitter import parse_source
from ..typescript.parser import TypeScriptParser
from .routes import detect_nest_routes


class NestJSParser(TypeScriptParser):
    name = "typescript-nestjs"
    overrides = ("typescript",)
    frameworks = ["nestjs"]

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        grammar = "tsx" if ctx.path.endswith((".tsx", ".jsx")) else "typescript"
        root = parse_source(grammar, ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)  # inherited base extraction (one parse)
        routes = detect_nest_routes(
            root, ctx.source, ctx.path, seen_ids={s.id for s in record.statements}
        )
        if routes:
            record.statements.extend(routes)
            record.framework = "nestjs"
        return record
