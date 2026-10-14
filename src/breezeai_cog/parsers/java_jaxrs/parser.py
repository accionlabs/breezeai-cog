"""JaxRsParser — a Java framework parser. Selected (one parser per file) over
JavaParser when ``claims`` finds a ``javax.ws.rs`` / ``jakarta.ws.rs`` import; reuses
``JavaParser.extract`` (single parse), then detects JAX-RS routes from the captured
annotations. Covers Jakarta RESTful Web Services (javax and jakarta namespaces)."""

from __future__ import annotations

from ...schemas import FileRecord
from ..base import ParseContext
from ..emits import apply
from ..java.config import module_of
from ..java.imports import JavaIndex
from ..java.parser import JavaParser
from ..java.web_guards import module_guards
from ..treesitter import parse_source
from .prefixes import declared_paths, jaxrs_prefixes
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
        if ctx.capture_statements:  # routes are statements — gated by statement capture
            idx = ctx.resolution_index
            module = module_of(ctx.path)
            prefixes = jaxrs_prefixes(idx)
            app_path = prefixes.app_paths.get(module, "") if module is not None else ""
            mg = module_guards(idx).get(module) if module is not None else None
            routes = detect_jaxrs_routes(
                record, root, ctx.source, app_path, prefixes.sub_bases,
                mg.jaxrs_filters if mg is not None else [],
                declared_paths(root, ctx.source, idx.consts if isinstance(idx, JavaIndex) else {}),
            )
            if routes:
                apply(record, routes, ctx.source, ctx.path)
                record.framework = "jaxrs"
        return record
