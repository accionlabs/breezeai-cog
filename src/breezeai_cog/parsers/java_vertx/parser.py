"""VertxParser — a Java framework parser. Selected (one parser per file) over JavaParser
when ``claims`` finds an ``io.vertx`` import; reuses ``JavaParser.extract`` (single parse),
then detects Vert.x event/messaging/route statements. Like all route/event detection,
gated by ``--capture-statements``."""

from __future__ import annotations

from ...schemas import FileRecord
from ..base import ParseContext
from ..java.parser import JavaParser
from ..treesitter import parse_source
from .events import detect_vertx


class VertxParser(JavaParser):
    name = "java-vertx"
    priority = 10
    frameworks = ["vertx"]

    def claims(self, path: str, source: bytes) -> bool:
        return b"io.vertx" in source

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("java", ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)  # inherited Java extraction (one parse)
        if ctx.capture_statements:  # events/routes are statements — gated by --capture-statements
            if detect_vertx(root, ctx.source, ctx.path, record):
                record.framework = "vertx"
        return record
