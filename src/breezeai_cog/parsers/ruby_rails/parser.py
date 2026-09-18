"""Rails framework parser selected over the base Ruby parser."""

from __future__ import annotations

from ...schemas import FileRecord
from ..base import ParseContext
from ..ruby.parser import RubyParser
from ..treesitter import parse_source
from ..ruby.claims import has_call, has_require, has_superclass
from .routes import detect_rails_routes


class RailsParser(RubyParser):
    name = "ruby-rails"
    priority = 30
    frameworks = ["rails"]

    def claims(self, path: str, source: bytes) -> bool:
        root = parse_source("ruby", source).root_node
        return (
            has_require(root, source, {"rails", "action_controller/railtie"})
            or has_superclass(root, source, {"ApplicationController"})
            or has_call(root, source, "Rails.application.routes", "draw")
        )

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("ruby", ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)
        if ctx.capture_statements and not self.is_fixture_file(ctx.path):
            if detect_rails_routes(root, ctx.source, ctx.path, record):
                record.framework = "rails"
        return record
