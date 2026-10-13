"""GraphQLCodeFirstParser — a TypeScript framework parser for code-first GraphQL whose operation
decorators (``@Query``/``@Mutation``/``@Subscription``) come from ``type-graphql``, or from an
in-house framework that defines them locally in a project-relative ``decorators`` module
(resolver classes are plain ``@Service`` DI classes, not ``@Resolver``). A sibling of the other TS
framework parsers — selected per file by ``claims`` + ``priority``. See routes.py for why
the NestJS and resolver-map/SDL detectors do not cover this style."""

from __future__ import annotations

import re

from ...schemas import FileRecord
from ..base import ParseContext
from ..emits import apply
from ..treesitter import parse_source
from ..typescript.parser import TypeScriptParser
from .routes import detect_codefirst_graphql_routes

# Decisive, collision-proof signature: the operation decorators are imported from a
# project-relative ``decorators`` module AND applied as method decorators. This excludes
# the framework's own ``decorators.ts`` (which *defines* them, never applies them) and
# never matches ``@nestjs/graphql`` files (they import from ``@nestjs/``).
_DECORATORS_IMPORT = re.compile(rb"""from\s+['"](?:[^'"]*\bdecorators|type-graphql)['"]""")
_OP_USAGE = re.compile(rb"@(?:Query|Mutation|Subscription)\s*\(")


class GraphQLCodeFirstParser(TypeScriptParser):
    name = "typescript-graphql-codefirst"
    priority = 15  # above graphql/express/angular (10); below nestjs (20)
    frameworks = ["graphql"]

    def claims(self, path: str, source: bytes) -> bool:
        return bool(_DECORATORS_IMPORT.search(source) and _OP_USAGE.search(source))

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        grammar = "tsx" if ctx.path.endswith((".tsx", ".jsx")) else "typescript"
        root = parse_source(grammar, ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)  # inherited base extraction (one parse)
        if ctx.capture_statements and not self.is_fixture_file(ctx.path):
            routes = detect_codefirst_graphql_routes(
                root, ctx.source, ctx.path, timeout_micros=ctx.parse_timeout_micros,
            )
            if routes:
                apply(record, routes, ctx.source, ctx.path)
                record.framework = "graphql"
        return record
