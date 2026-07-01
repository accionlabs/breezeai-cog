"""SpringBootParser — a Java framework parser. Selected (one parser per file) over
JavaParser when ``claims`` finds an ``org.springframework`` import; reuses
``JavaParser.extract`` (single parse), then detects Spring MVC routes from the captured
annotations. Covers Spring Boot v2 and v3 (identical web annotations)."""

from __future__ import annotations

from ...schemas import FileRecord
from ..base import ParseContext
from ..java.parser import JavaParser
from ..treesitter import parse_source
from .queries import detect_spring_queries
from .routes import detect_spring_routes


class SpringBootParser(JavaParser):
    name = "java-springboot"
    priority = 10
    frameworks = ["spring", "springboot"]

    def claims(self, path: str, source: bytes) -> bool:
        return b"org.springframework" in source

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("java", ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)  # inherited Java extraction (one parse)
        if ctx.capture_statements:  # routes/queries are statements — gated (spec A4)
            routes = detect_spring_routes(record)  # off the record — no AST re-walk
            if routes:
                record.statements.extend(routes)
                record.framework = "spring"
            record.statements.extend(detect_spring_queries(record))  # @Query → query_statement
        return record
