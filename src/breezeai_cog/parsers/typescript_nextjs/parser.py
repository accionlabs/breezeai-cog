"""NextJSParser — a TypeScript framework parser for **Next.js App Router** route handlers.
Selected over the base TypeScriptParser (single parser per file) when ``claims`` finds an
``app/**/route.*`` file that exports an HTTP-verb function; reuses ``TypeScriptParser.extract``
on the shared tree, then adds routes (off the record — see ``routes.py``). It coexists with
the other TS framework parsers because selection is per-file by ``claims``: a Next.js route
file carries no ``@Controller``/``react-router`` signature, so only this parser claims it."""

from __future__ import annotations

import re

from ...schemas import FileRecord
from ..base import ParseContext
from ..treesitter import parse_source
from ..typescript.parser import TypeScriptParser
from .routes import detect_nextjs_routes, is_app_router_route_file, is_pages_api_file

# A verb handler in either accepted form: ``export … function GET`` / ``export const GET =``.
_VERB_EXPORT_SIG = re.compile(
    rb"export\s+(?:async\s+)?(?:function\s+|(?:const|let|var)\s+)"
    rb"(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b"
)
# A Pages Router handler is the file's default export (any shape).
_DEFAULT_EXPORT_SIG = re.compile(rb"export\s+default\b")


class NextJSParser(TypeScriptParser):
    name = "typescript-nextjs"
    priority = 10
    frameworks = ["nextjs"]

    def claims(self, path: str, source: bytes) -> bool:
        # Path-gated so we don't claim UI files that merely live under app/ or pages/, plus a
        # cheap byte sniff for the handler shape each router uses.
        if is_app_router_route_file(path):
            return bool(_VERB_EXPORT_SIG.search(source))
        if is_pages_api_file(path):
            return bool(_DEFAULT_EXPORT_SIG.search(source))
        return False

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        grammar = "tsx" if ctx.path.endswith((".tsx", ".jsx")) else "typescript"
        root = parse_source(grammar, ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)  # inherited base extraction (one parse)
        if ctx.capture_statements and not self.is_fixture_file(ctx.path):  # gated; skip fixtures
            routes = detect_nextjs_routes(
                root,
                ctx.source,
                ctx.path,
                record,
                seen_ids={s.id for s in record.statements},
            )
            if routes:
                record.statements.extend(routes)
                record.framework = "nextjs"
        return record
