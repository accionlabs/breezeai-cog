"""ServiceHostParser — parses .NET endpoint-host markup: WCF ``.svc`` (``ServiceHost``) and
ASMX ``.asmx`` (``WebService``) files.

Both are a single ``<%@ … %>`` directive naming the concrete class behind an endpoint (with a
``CodeBehind`` to its ``.cs``). The ``.svc.cs``/``.asmx.cs`` code-behind is ordinary C# (parsed
by CSharpParser/WcfParser); the markup has no C# body, so this is a directive scan, not a
tree-sitter parse — hence a standalone ``BaseParser`` (not a ``CSharpParser`` subclass). Its
value is the class FQN (``Service=``/``Class=``): the concrete impl wired to the endpoint,
which resolves the interface/URL → concrete-class ambiguity ``.cs`` alone cannot.

This parser owns both extensions so the scanner stops dropping them as ``unsupported``. The
per-type differences live in :data:`~.routes.DIRECTIVES`; directive parse + ``route`` emission
is in :mod:`.routes`.
"""

from __future__ import annotations

from ...emit import file_id
from ...schemas import SCHEMA_VERSION, FileRecord
from ...utils import count_loc
from ..base import BaseParser, ParseContext
from .routes import DIRECTIVES, detect_service_host, spec_for

_STATEMENT_TYPES = ["route"]


class ServiceHostParser(BaseParser):
    name = "dotnet-servicehost"
    extensions = tuple(DIRECTIVES)  # (".svc", ".asmx")
    schema_version = SCHEMA_VERSION
    statement_types = _STATEMENT_TYPES
    frameworks = ["wcf", "asmx"]

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        text = ctx.source.decode("utf-8", "replace")
        spec = spec_for(ctx.path)
        # language is per-type (svc/asmx): a host file is its own artifact (markup, not C#), so
        # it gets a distinct label rather than inflating the C# LOC/file rollups it sits beside.
        record = FileRecord(
            id=file_id(ctx.path),
            path=ctx.path,
            type="code",
            language=spec.language if spec is not None else "svc",
            loc=count_loc(text),
        )
        if ctx.capture_statements:  # the route is a semantic statement — gated (spec A4)
            stmt, imports = detect_service_host(ctx.source, ctx.path, ctx.repo_root, seen_ids=set())
            if stmt is not None:
                record.statements.append(stmt)
                record.framework = stmt.framework
            record.importFiles = imports
        return record
