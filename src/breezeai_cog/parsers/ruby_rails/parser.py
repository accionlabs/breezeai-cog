"""Rails framework parser selected over the base Ruby parser."""

from __future__ import annotations

from ...schemas import FileRecord
from ..base import ParseContext
from ..ruby.parser import RubyParser
from ..treesitter import parse_source
from .routes import detect_rails_routes


class RailsParser(RubyParser):
    name = "ruby-rails"
    priority = 10
    frameworks = ["rails"]

    def claims(self, path: str, source: bytes) -> bool:
        return (
            b"require \"rails\"" in source
            or b"require 'rails'" in source
            or b"action_controller/railtie" in source
            or b"Rails.application.routes.draw" in source
            or b"ApplicationController" in source
        )

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("ruby", ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)
        if ctx.capture_statements and not self.is_fixture_file(ctx.path):
            if detect_rails_routes(root, ctx.source, ctx.path, record):
                record.framework = "rails"
        return record
