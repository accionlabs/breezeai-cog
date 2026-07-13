"""CSharpGraphQLParser — a C# framework parser for graphql-dotnet (code-first).

A sibling of :class:`AspNetCoreParser` — both subclass ``CSharpParser`` and are chosen per
file by ``claims`` + ``priority`` (one parser per file). This parser is selected when a file
declares a graphql-dotnet schema type (``ObjectGraphType``); ASP.NET controller / minimal-API
files fall to ``AspNetCoreParser``. GraphQL schema-type files and ASP.NET route files are
disjoint by the framework's design (a class derives from ``ObjectGraphType`` *or*
``ControllerBase``), so there is nothing to compose — hence the clean sibling split rather
than chaining off the ASP.NET parser. HotChocolate (attribute-based) is out of scope."""

from __future__ import annotations

from ...schemas import FileRecord
from ..base import ParseContext
from ..csharp.parser import CSharpParser
from ..treesitter import parse_source
from .routes import detect_graphql_dotnet_routes

_MARKERS = (b"ObjectGraphType",)


class CSharpGraphQLParser(CSharpParser):
    name = "csharp-graphql"
    priority = 20  # selected over the base C# parser (0) when it claims a schema file
    frameworks = ["graphql"]

    def claims(self, path: str, source: bytes) -> bool:
        return any(m in source for m in _MARKERS)

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("csharp", ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)  # inherited C# extraction (one parse)
        if ctx.capture_statements and not self.is_fixture_file(ctx.path):
            seen = {s.id for s in record.statements}
            routes = detect_graphql_dotnet_routes(record, root, ctx.source, ctx.path, seen)
            if routes:
                record.statements.extend(routes)
                record.framework = "graphql"
        return record
