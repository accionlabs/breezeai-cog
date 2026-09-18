"""Grape framework parser selected over the base Ruby parser."""

from __future__ import annotations

from ...schemas import FileRecord
from ..base import ParseContext
from ..ruby.parser import RubyParser
from ..treesitter import parse_source
from ..ruby.claims import has_require, has_superclass
from .routes import detect_grape_routes


class GrapeParser(RubyParser):
    name = "ruby-grape"
    priority = 20
    frameworks = ["grape"]

    def claims(self, path: str, source: bytes) -> bool:
        root = parse_source("ruby", source).root_node
        return (
            has_require(root, source, {"grape"})
            or has_superclass(root, source, {"Grape::API", "Grape::Endpoint"})
        )

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("ruby", ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)
        if ctx.capture_statements and not self.is_fixture_file(ctx.path):
            if detect_grape_routes(root, ctx.source, ctx.path, record):
                record.framework = "grape"
        return record
