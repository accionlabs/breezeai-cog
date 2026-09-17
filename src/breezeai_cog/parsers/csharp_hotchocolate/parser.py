"""CSharpHotChocolateParser — a C# framework parser for HotChocolate (annotation-based).

A sibling of :class:`CSharpGraphQLParser` (graphql-dotnet) and :class:`AspNetCoreParser`: all
three subclass ``CSharpParser`` and exactly one is chosen per file by ``claims`` + ``priority``.
The two GraphQL parsers describe different libraries — graphql-dotnet declares its schema by
deriving from ``ObjectGraphType``, HotChocolate by attributes — so their byte guards are
disjoint *and* the priorities differ, because ``registry.select`` breaks a priority tie by
registration order, which would make selection arbitrary.

``Program.cs`` is deliberately **not** claimed: ``AddGraphQLServer()`` / ``MapGraphQL()`` live in
a file owned by ``csharp-aspnet``, and taking it would drop the application's REST routes. The
GraphQL endpoint mount belongs there as additive detection instead.
"""

from __future__ import annotations

from ...schemas import FileRecord
from ..base import ParseContext
from ..csharp.parser import CSharpParser
from ..treesitter import parse_source
from .mappings import MARKERS
from .routes import detect_hotchocolate_routes

#: graphql-dotnet's marker. A file declaring one is that library's, not this one's.
_GRAPHQL_DOTNET_MARKER = b"ObjectGraphType"


class CSharpHotChocolateParser(CSharpParser):
    name = "csharp-hotchocolate"
    priority = 25  # above csharp-aspnet (10); distinct from csharp-graphql (20) — no tie
    frameworks = ["graphql"]

    def claims(self, path: str, source: bytes) -> bool:
        if _GRAPHQL_DOTNET_MARKER in source:
            return False  # graphql-dotnet owns it; keep the two guards mutually exclusive
        return any(m in source for m in MARKERS)

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("csharp", ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)  # inherited C# extraction (one parse)
        if ctx.capture_statements and not self.is_fixture_file(ctx.path):
            seen = {s.id for s in record.statements}
            routes = detect_hotchocolate_routes(record, seen)
            if routes:
                record.statements.extend(routes)
                record.framework = "graphql"
        return record
