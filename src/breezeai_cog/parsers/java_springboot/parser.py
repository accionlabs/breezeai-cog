"""SpringBootParser — a Java framework parser. Selected (one parser per file) over
JavaParser when ``claims`` finds an ``org.springframework`` import; reuses
``JavaParser.extract`` (single parse), then detects Spring MVC routes from the captured
annotations. Covers Spring Boot v2 and v3 (identical web annotations). The module's
``server.servlet.context-path`` (read by the Java index) prefixes every HTTP route."""

from __future__ import annotations

from ...schemas import FileRecord
from ..base import ParseContext
from ..java.config import module_of
from ..java.imports import JavaIndex
from ..java.parser import JavaParser
from ..treesitter import parse_source
from .functional_routes import detect_spring_functional_routes
from .queries import detect_spring_queries
from .routes import apply_context_path, apply_global_guards, detect_spring_routes


class SpringBootParser(JavaParser):
    name = "java-springboot"
    priority = 10
    frameworks = ["spring", "springboot"]

    def claims(self, path: str, source: bytes) -> bool:
        return b"org.springframework" in source

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("java", ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)  # inherited Java extraction (one parse)
        if ctx.capture_statements:  # routes/queries are statements — gated by statement capture
            routes = detect_spring_routes(record)  # annotated controllers — off the record
            if b"RouterFunction" in ctx.source:  # functional routing needs the AST
                routes += detect_spring_functional_routes(root, ctx.source, ctx.path, record)
            if routes:
                idx = ctx.resolution_index
                module = module_of(ctx.path)
                # Servlet filters and MVC interceptors of this module run first (matched within
                # the servlet context, so before the context path is joined).
                guards = idx.module_guards if isinstance(idx, JavaIndex) else {}
                if module is not None and module in guards:
                    apply_global_guards(routes, guards[module])
                # server.servlet.context-path of this file's module prefixes every HTTP route
                paths = idx.context_paths if isinstance(idx, JavaIndex) else {}
                if module is not None and module in paths:
                    apply_context_path(routes, paths[module])
                record.statements.extend(routes)
                record.framework = "spring"
            record.statements.extend(detect_spring_queries(record))  # @Query → query_statement
        return record
