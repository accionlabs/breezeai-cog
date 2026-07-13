"""JaxRsParser — a Java framework parser. Selected (one parser per file) over
JavaParser when ``claims`` finds a ``javax.ws.rs`` / ``jakarta.ws.rs`` import; reuses
``JavaParser.extract`` (single parse), then detects JAX-RS routes from the captured
annotations. Covers Jakarta RESTful Web Services (javax and jakarta namespaces)."""

from __future__ import annotations

from ...schemas import FileRecord
from ..base import ParseContext
from ..java.parser import JavaParser
from ..treesitter import parse_source
from .routes import detect_jaxrs_routes


class JaxRsParser(JavaParser):
    name = "java-jaxrs"
    priority = 10
    frameworks = ["jaxrs"]

    def claims(self, path: str, source: bytes) -> bool:
        return b"javax.ws.rs" in source or b"jakarta.ws.rs" in source

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("java", ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)  # inherited Java extraction (one parse)
        if ctx.capture_statements:  # routes are statements — gated by --capture-statements
            routes = detect_jaxrs_routes(record)  # off the record — no AST re-walk
            if routes:
                record.statements.extend(routes)
                record.framework = "jaxrs"
        return record
