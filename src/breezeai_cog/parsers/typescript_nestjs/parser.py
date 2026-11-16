"""NestJSParser — a TypeScript framework parser for **decorator-declared routes**.
Selected over the base TypeScriptParser (single parser per file) when ``claims`` finds a
NestJS *or* routing-controllers signature; reuses ``TypeScriptParser.extract`` on the
shared tree, then adds routes. Both frameworks share the same ``@Controller`` + ``@Get``/
``@Post`` decorator grammar, so the same detector serves both — only the ``framework``
label differs. It coexists with other TS framework parsers (Angular) because selection is
per-file by ``claims``."""

from __future__ import annotations

from ...schemas import FileRecord
from ..base import ParseContext
from ..emits import apply
from ..treesitter import parse_source
from ..typescript.parser import TypeScriptParser
from .outbound import nest_framework
from .patterns import claims_nest
from .routes import detect_nest_routes

class NestJSParser(TypeScriptParser):
    name = "typescript-nestjs"
    # Above the other TypeScript framework parsers (priority 10): a decorator-controller
    # signature is decisive, so this must win the selection. Express routes in the same file
    # still come from the additive ``express`` detector.
    priority = 20
    frameworks = ["nestjs", "routing-controllers", "nestjs-like"]

    def claims(self, path: str, source: bytes) -> bool:
        return claims_nest(source)

    @staticmethod
    def _framework(source: bytes) -> str:
        return nest_framework(source)

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        grammar = "tsx" if ctx.path.endswith((".tsx", ".jsx")) else "typescript"
        root = parse_source(grammar, ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)  # inherited base extraction (one parse)
        if ctx.capture_statements and not self.is_fixture_file(ctx.path):  # gated by statement capture; skip fixtures (R4)
            framework = self._framework(ctx.source)
            routes = detect_nest_routes(
                root, ctx.source, ctx.path, framework=framework, index=ctx.resolution_index,
            )
            if routes:
                apply(record, routes, ctx.source, ctx.path)
                record.framework = framework
            # Outbound microservice messages (ClientProxy.send / emit) are the additive
            # ``nestjs-clients`` detector (outbound.py): they run in every owner's extract().
        return record
