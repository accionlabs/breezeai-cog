"""ExpressParser — a TypeScript/JavaScript framework parser. Selected over the base
TypeScriptParser (single parser per file) when ``claims`` finds an ``express`` import;
reuses ``TypeScriptParser.extract`` on the shared tree, then detects Express routes.
It coexists with the other TS framework parsers (NestJS, Angular, LoopBack, React)
because selection is per-file by ``claims`` (ARCHITECTURE.md §4). Express is call-based,
so route detection enriches the base statement in place (mirrors ``java_vertx``)."""

from __future__ import annotations

from ...schemas import FileRecord
from ..base import ParseContext
from ..treesitter import parse_source
from ..typescript.parser import TypeScriptParser
from .routes import detect_express


class ExpressParser(TypeScriptParser):
    name = "typescript-express"
    priority = 10
    frameworks = ["express"]

    def claims(self, path: str, source: bytes) -> bool:
        # Precise import sniff — ``require('express')`` / ``from 'express'`` (either quote
        # style). The trailing quote excludes ``'express-session'`` and friends.
        return b"'express'" in source or b'"express"' in source

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        grammar = "tsx" if ctx.path.endswith((".tsx", ".jsx")) else "typescript"
        root = parse_source(grammar, ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)  # inherited base extraction (one parse)
        if ctx.capture_statements:  # routes are statements — gated by --capture-statements (spec A4)
            if detect_express(root, ctx.source, ctx.path, record):
                record.framework = "express"
        return record
