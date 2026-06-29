"""SpringBootParser — a Java framework parser that overrides JavaParser. Single parse,
reuses ``JavaParser.extract``, then detects Spring MVC routes from the captured
annotations. Covers Spring Boot v2 and v3 (identical web annotations)."""

from __future__ import annotations

from ...schemas import FileRecord
from ..base import ParseContext
from ..java.parser import JavaParser
from ..treesitter import parse_source
from .routes import detect_spring_routes


class SpringBootParser(JavaParser):
    name = "java-springboot"
    overrides = ("java",)
    frameworks = ["spring", "springboot"]

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("java", ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)  # inherited Java extraction (one parse)
        routes = detect_spring_routes(record)  # off the record — no AST re-walk
        if routes:
            record.statements.extend(routes)
            record.framework = "spring"
        return record
