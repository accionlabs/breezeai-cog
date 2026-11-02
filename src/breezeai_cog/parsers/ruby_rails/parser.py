"""Rails framework parser selected over the base Ruby parser."""

from __future__ import annotations

from ...schemas import FileRecord
from ..base import ParseContext
from ..emits import apply
from ..ruby.parser import RubyParser
from ..treesitter import parse_source
from ..ruby.claims import has_require, has_superclass
from .routes import detect_rails_routes, has_routes_draw


class RailsParser(RubyParser):
    name = "ruby-rails"
    priority = 30
    frameworks = ["rails"]

    def claims(self, path: str, source: bytes, parse_timeout_micros: int = 0) -> bool:
        if not any(marker in source for marker in (
            b"rails", b"Rails", b"ApplicationController", b"Rails.application.routes",
            b"Application.routes",
        )):
            return False
        root = parse_source("ruby", source, parse_timeout_micros).root_node
        return (
            has_require(root, source, {"rails", "action_controller/railtie"})
            or has_superclass(root, source, {"ApplicationController"})
            or has_routes_draw(root, source)
        )

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("ruby", ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)
        if ctx.capture_statements and not self.is_fixture_file(ctx.path):
            routes = detect_rails_routes(root, ctx.source, ctx.path, record, ctx.resolution_index)
            if routes:
                apply(record, routes, ctx.source, ctx.path)
                record.framework = "rails"
        return record
