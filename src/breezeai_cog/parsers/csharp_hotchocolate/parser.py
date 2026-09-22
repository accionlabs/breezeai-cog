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
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Sequence

from ...schemas import FileRecord
from ..base import ParseContext
from ..csharp.parser import CSharpParser
from ..treesitter import parse_source
from .mappings import COMPOSITION_ROOT_MARKERS, MARKERS
from .routes import detect_hotchocolate_routes

#: graphql-dotnet's marker. A file declaring one is that library's, not this one's.
_GRAPHQL_DOTNET_MARKER = b"ObjectGraphType"

#: Matches .AddQueryType<ClassName>(), .AddMutationType<ClassName>(), .AddSubscriptionType<ClassName>().
_REGISTRATION_RE = re.compile(rb"\.Add(Query|Mutation|Subscription)Type<(\w+)>\s*\(")
_REGISTRATION_KINDS: dict[bytes, str] = {
    b"Query": "query",
    b"Mutation": "mutation",
    b"Subscription": "subscription",
}


def _scan_root_registrations(files: Sequence[Path]) -> dict[str, str]:
    """Scan composition root files for AddQueryType/MutationType/SubscriptionType<T>()
    registrations. Returns a map of simple class name → operation kind. Only files
    containing ``AddGraphQLServer`` are read (the composition-root byte guard)."""
    root_types: dict[str, str] = {}
    for f in files:
        if f.suffix != ".cs":
            continue
        try:
            src = f.read_bytes()
        except OSError:
            continue
        if b"AddGraphQLServer" not in src:
            continue
        for m in _REGISTRATION_RE.finditer(src):
            kind = _REGISTRATION_KINDS.get(m.group(1))
            name = m.group(2).decode()
            if kind and name not in root_types:
                root_types[name] = kind
    return root_types


class CSharpHotChocolateParser(CSharpParser):
    name = "csharp-hotchocolate"
    priority = 25  # above csharp-aspnet (10); distinct from csharp-graphql (20) — no tie
    frameworks = ["graphql"]

    def __init__(self) -> None:
        # Populated by build_index when the repo pre-pass runs; empty in single-file / test mode.
        self._hc_root_types: dict[str, str] = {}

    def build_index(self, repo_root: Path, files: Sequence[Path], jobs: int = 1):
        index = super().build_index(repo_root, files, jobs)
        self._hc_root_types = _scan_root_registrations(files)
        return index

    def claims(self, path: str, source: bytes) -> bool:
        if _GRAPHQL_DOTNET_MARKER in source:
            return False  # graphql-dotnet owns it; keep the two guards mutually exclusive
        if any(m in source for m in COMPOSITION_ROOT_MARKERS):
            return False  # the composition root stays with csharp-aspnet (see mappings)
        if any(m in source for m in MARKERS):
            return True
        # Also claim files that declare a class registered as a root via AddQueryType<T>() but
        # carrying no root attribute. The byte guard ``class ClassName`` matches only the
        # declaring file (a reference uses the name without the ``class`` keyword).
        return any(b"class " + n.encode() in source for n in self._hc_root_types)

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("csharp", ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)  # inherited C# extraction (one parse)
        if ctx.capture_statements and not self.is_fixture_file(ctx.path):
            seen = {s.id for s in record.statements}
            routes = detect_hotchocolate_routes(
                record, root, ctx.source, seen, ctx.resolution_index,
                hc_root_types=self._hc_root_types or None)
            if routes:
                record.statements.extend(routes)
                record.framework = "graphql"
        return record
