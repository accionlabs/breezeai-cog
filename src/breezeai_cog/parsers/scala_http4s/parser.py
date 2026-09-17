"""``ScalaHttp4sParser`` — http4s case-pattern routes, layered over ``ScalaParser``."""

from __future__ import annotations

from ...schemas import FileRecord
from ..base import ParseContext
from ..scala.parser import ScalaParser
from ..treesitter import parse_source
from .routes import detect_http4s_routes


class ScalaHttp4sParser(ScalaParser):
    name = "scala-http4s"
    priority = 10
    frameworks = ["http4s"]

    def claims(self, path: str, source: bytes) -> bool:
        return b"org.http4s" in source

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("scala", ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)
        if ctx.capture_statements and not self.is_fixture_file(ctx.path):
            routes = detect_http4s_routes(root, ctx.source, ctx.path, record)
            if routes:
                record.statements.extend(routes)
                record.framework = "http4s"
        return record
