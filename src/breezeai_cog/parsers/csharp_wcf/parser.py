"""WcfParser — a C# framework parser for Windows Communication Foundation (WCF) service
contracts. Selected over CSharpParser when ``claims`` sees a ``System.ServiceModel`` usage,
EXCEPT on Web Forms code-behind (``.aspx.cs``/``.ascx.cs``/``.master.cs``) — those are a
page that merely *consumes* a WCF client and belong to WebFormsParser, so the two claim
disjoint files. Reuses ``CSharpParser.extract`` (single parse), then reads
``[ServiceContract]`` / ``[OperationContract]`` off the captured decorators (no AST re-walk,
like the ASP.NET Core controller detector) to emit one ``route`` statement per operation."""

from __future__ import annotations

from ...schemas import FileRecord
from ..base import ParseContext
from ..csharp.parser import CSharpParser
from ..treesitter import parse_source
from .routes import detect_asmx_services, detect_wcf_services

#: SOAP-service markers: WCF (System.ServiceModel), CoreWCF (the .NET Core port), and legacy
#: ASMX (System.Web.Services). Web Forms code-behind (System.Web.UI) is claimed elsewhere.
_MARKERS = (b"System.ServiceModel", b"CoreWCF", b"System.Web.Services")
_WEBFORMS_SUFFIXES = (".aspx.cs", ".ascx.cs", ".master.cs")


class WcfParser(CSharpParser):
    name = "csharp-wcf"
    priority = 10  # framework parser > base csharp (0); claims disjoint from webforms/aspnet
    frameworks = ["wcf", "asmx"]

    def claims(self, path: str, source: bytes) -> bool:
        if path.endswith(_WEBFORMS_SUFFIXES):
            return False  # a WCF-consuming page is WebFormsParser's file, not ours
        return any(m in source for m in _MARKERS)

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("csharp", ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)  # inherited C# extraction (one parse)
        if ctx.capture_statements:  # routes are statements — gated (spec A4)
            routes = detect_wcf_services(record) or detect_asmx_services(record)
            if routes:
                record.statements.extend(routes)
                record.framework = routes[0].framework  # "wcf" or "asmx"
        return record
