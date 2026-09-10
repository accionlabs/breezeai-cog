"""``ScalaAkkaHttpParser`` — akka-http directive DSL routes, layered over ``ScalaParser``."""

from __future__ import annotations

from ...schemas import FileRecord
from ..base import ParseContext
from ..scala.parser import ScalaParser
from ..treesitter import parse_source
from .routes import detect_akkahttp_routes


class ScalaAkkaHttpParser(ScalaParser):
    name = "scala-akka-http"
    priority = 10
    frameworks = ["akka-http"]

    def claims(self, path: str, source: bytes) -> bool:
        return b"akka.http.scaladsl" in source

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("scala", ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)
        if ctx.capture_statements and not self.is_fixture_file(ctx.path):
            routes = detect_akkahttp_routes(root, ctx.source, ctx.path, record)
            if routes:
                record.statements.extend(routes)
                record.framework = "akka-http"
        return record
