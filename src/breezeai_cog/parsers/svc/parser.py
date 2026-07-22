"""SvcHostParser — parses a WCF ``.svc`` **ServiceHost** file.

A ``.svc`` is the endpoint-host markup IIS serves for a WCF service — a single directive::

    <%@ ServiceHost Service="KUCare.Services.AttendanceService" Factory="…" CodeBehind="…" %>

The ``.svc.cs`` code-behind is ordinary C# (parsed by CSharpParser/WcfParser); the ``.svc``
markup itself has no C# body, so this is **not** a C# tree-sitter parse — it is a lightweight
directive scan (like the config parser and the Web Forms markup pass). Its value is the
``Service`` attribute: the **concrete implementation FQN** wired to the endpoint, which
resolves the ``[ServiceContract]`` interface → concrete-class ambiguity that ``.cs`` alone
cannot (an interface may have several implementers; only the host says which one is served).

This parser owns the ``.svc`` extension so the scanner stops dropping it as ``unsupported``.
The directive parse + ``route`` emission lands in :mod:`.routes` (kept separate so this file
stays a thin selection/registration shell, mirroring the other framework parsers).
"""

from __future__ import annotations

from ...emit import file_id
from ...schemas import SCHEMA_VERSION, FileRecord
from ...utils import count_loc
from ..base import BaseParser, ParseContext
from .routes import detect_svc_host

_STATEMENT_TYPES = ["route"]


class SvcHostParser(BaseParser):
    name = "svc"
    extensions = (".svc",)
    schema_version = SCHEMA_VERSION
    statement_types = _STATEMENT_TYPES
    frameworks = ["wcf"]

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        text = ctx.source.decode("utf-8", "replace")
        # language="svc": a ServiceHost file is its own artifact (markup, not C#), so it gets
        # a distinct label rather than inflating the C# LOC/file rollups it sits beside.
        record = FileRecord(
            id=file_id(ctx.path),
            path=ctx.path,
            type="code",
            language="svc",
            loc=count_loc(text),
        )
        if ctx.capture_statements:  # the route is a semantic statement — gated (spec A4)
            stmt, imports = detect_svc_host(ctx.source, ctx.path, ctx.repo_root, seen_ids=set())
            if stmt is not None:
                record.statements.append(stmt)
                record.framework = "wcf"
            record.importFiles = imports
        return record
