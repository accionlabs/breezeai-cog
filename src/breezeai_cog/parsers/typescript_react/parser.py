"""ReactParser — a TypeScript framework parser. Selected over the base TypeScriptParser
(single parser per file) when ``claims`` finds a ``react-router`` import; reuses
``TypeScriptParser.extract`` on the shared tree, then adds React Router routes. It
coexists with the other TS framework parsers (NestJS, Angular, LoopBack) because
selection is per-file by ``claims``."""

from __future__ import annotations

from ...schemas import FileRecord
from ..base import ParseContext
from ..treesitter import parse_source
from ..typescript.parser import TypeScriptParser
from .components import mark_react_components
from .routes import detect_react_routes

# Byte guards for a React file: a router import (routes) OR a bare ``react`` import — the
# latter is what a plain component file (``Button.tsx``) has, and is needed so components
# reach this parser at all. ``'react'`` / ``"react"`` match ``from 'react'`` exactly and do
# NOT match ``react-dom`` / ``react-router-dom`` (more chars precede the closing quote).
_REACT_GUARDS = (b"react-router", b"'react'", b'"react"')


class ReactParser(TypeScriptParser):
    name = "typescript-react"
    priority = 10
    frameworks = ["react"]

    def claims(self, path: str, source: bytes) -> bool:
        return any(g in source for g in _REACT_GUARDS)

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        grammar = "tsx" if ctx.path.endswith((".tsx", ".jsx")) else "typescript"
        root = parse_source(grammar, ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)  # inherited base extraction (one parse)
        # Class components (`extends React.Component`) and function components (PascalCase +
        # renders JSX) -> uiRole. Nodes are always captured, so this is not gated on statements.
        mark_react_components(root, ctx.source, record)
        # framework axis: every file this parser claims IS a React file — stamp it
        # unconditionally so "list React files" is answerable, not only on route files. The
        # JS/TS distinction lives on the orthogonal `language` axis (.tsx -> typescript,
        # .jsx -> javascript), set by the base extractor.
        record.framework = "react"
        if ctx.capture_statements and not self.is_fixture_file(ctx.path):  # gated by --capture-statements; skip fixtures (R4)
            routes = detect_react_routes(
                root, ctx.source, ctx.path, seen_ids={s.id for s in record.statements}
            )
            if routes:
                record.statements.extend(routes)  # framework already stamped above
        return record
