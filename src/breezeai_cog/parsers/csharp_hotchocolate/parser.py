"""CSharpHotChocolateParser — a C# framework parser for HotChocolate (annotation-based).

A sibling of :class:`CSharpGraphQLParser` (graphql-dotnet) and :class:`AspNetCoreParser`: all
three subclass ``CSharpParser`` and exactly one is chosen per file by ``claims`` + ``priority``.
The two GraphQL parsers describe different libraries — graphql-dotnet declares its schema by
deriving from ``ObjectGraphType``, HotChocolate by attributes — so their byte guards are
disjoint *and* the priorities differ, because ``registry.select`` breaks a priority tie by
registration order, which would make selection arbitrary.

``Program.cs`` is deliberately **not** claimed: ``AddGraphQLServer()`` / ``MapGraphQL()`` live in a
file owned by ``csharp-aspnet``, which captures the application's REST endpoints and already emits
the GraphQL HTTP mount itself. Claiming it would trade the whole route inventory for nothing, so
the guard is positive on *schema declarations* and negative on the registration calls.

Residual gap: a root type declared **inside** the composition root yields no operations. A file
that both wires up the server and declares a schema root is a single-file demo shape; losing an
application's route inventory is the worse of the two failures.

Registration-only roots (``AddQueryType<BookQueries>()`` on an attribute-less class, the v11/v12
shape) are bound to their declaring file by the ``hotchocolate-registered-roots`` additive detector
(:mod:`.registered_roots`) while the C# index is built. Such a file carries no HotChocolate marker,
so this parser never claims it; the detector emits its operations instead. In a file this parser
does claim, it reads the same fact itself.
"""

from __future__ import annotations

from typing import Any

from ...schemas import FileRecord
from ..base import ParseContext
from ..csharp.parser import CSharpParser
from ..treesitter import parse_source
from .mappings import owned_by_hotchocolate
from .registered_roots import registered_roots
from .routes import detect_hotchocolate_routes


def _root_types(index: Any | None, path: str) -> dict[str, str] | None:
    """Registered roots visible from ``path``: this file's own registered classes, plus names
    that are registered roots repo-wide (for a target declared in another file)."""
    roots = registered_roots(index)
    return {**roots.types, **roots.files.get(path, {})} or None


class CSharpHotChocolateParser(CSharpParser):
    name = "csharp-hotchocolate"
    priority = 25  # above csharp-aspnet (10); distinct from csharp-graphql (20) — no tie
    frameworks = ["graphql"]

    def claims(self, path: str, source: bytes) -> bool:
        return owned_by_hotchocolate(source)

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("csharp", ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)  # inherited C# extraction (one parse)
        if ctx.capture_statements and not self.is_fixture_file(ctx.path):
            seen = {s.id for s in record.statements}
            routes = detect_hotchocolate_routes(
                record, root, ctx.source, seen, ctx.resolution_index,
                hc_root_types=_root_types(ctx.resolution_index, ctx.path))
            if routes:
                record.statements.extend(routes)
                record.framework = "graphql"
        return record
