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
shape) are resolved by the C# repo index (``hc_root_files`` / ``hc_root_types``), which binds each
registration to its declaring file the way the compiler binds the type. Such a file carries no
HotChocolate marker, so it is claimed through :meth:`claims_with_index`.
"""

from __future__ import annotations

from typing import Any

from ...schemas import FileRecord
from ..base import ParseContext
from ..csharp.parser import CSharpParser
from ..treesitter import parse_source
from .mappings import COMPOSITION_ROOT_MARKERS, MARKERS
from .routes import detect_hotchocolate_routes

#: graphql-dotnet's marker. A file declaring one is that library's, not this one's.
_GRAPHQL_DOTNET_MARKER = b"ObjectGraphType"


def _root_types(index: Any | None, path: str) -> dict[str, str] | None:
    """Registered roots visible from ``path``: this file's own registered classes, plus names
    that are registered roots repo-wide (for a target declared in another file)."""
    repo_wide = getattr(index, "hc_root_types", None) or {}
    own = (getattr(index, "hc_root_files", None) or {}).get(path, {})
    return {**repo_wide, **own} or None


class CSharpHotChocolateParser(CSharpParser):
    name = "csharp-hotchocolate"
    priority = 25  # above csharp-aspnet (10); distinct from csharp-graphql (20) — no tie
    frameworks = ["graphql"]

    def claims(self, path: str, source: bytes) -> bool:
        if _GRAPHQL_DOTNET_MARKER in source:
            return False  # graphql-dotnet owns it; keep the two guards mutually exclusive
        if any(m in source for m in COMPOSITION_ROOT_MARKERS):
            return False  # the composition root stays with csharp-aspnet (see mappings)
        return any(m in source for m in MARKERS)

    def claims_with_index(self, path: str, source: bytes, index: Any | None) -> bool:
        """Also claim a file declaring a registration-only root: a plain class, with nothing
        HotChocolate in it, that the composition root registers via ``AddQueryType<T>()``."""
        if self.claims(path, source):
            return True
        if _GRAPHQL_DOTNET_MARKER in source or any(m in source for m in COMPOSITION_ROOT_MARKERS):
            return False
        return path in (getattr(index, "hc_root_files", None) or {})

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
