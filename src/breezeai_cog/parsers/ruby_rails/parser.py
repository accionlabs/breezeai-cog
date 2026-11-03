"""Rails framework parser selected over the base Ruby parser."""

from __future__ import annotations

from ...schemas import FileRecord
from ..base import ParseContext
from ..emits import apply
from ..ruby.parser import RubyParser
from ..treesitter import parse_source
from .routes import detect_rails_routes

#: A file is Rails when its source says so: a controller (``< ApplicationController``), a route
#: file (``Rails.application.routes.draw``, or ``<App>::Application.routes.draw`` as Rails 3 and 4
#: write it), or a Rails require. A cheap substring search, so ``claims`` does not parse the file a
#: second time.
_CLAIM_MARKERS = (
    b"< ApplicationController", b"<ApplicationController",
    b"Rails.application.routes.draw", b"Application.routes.draw",
    b"require 'rails'", b'require "rails"',
    b"require 'action_controller/railtie'", b'require "action_controller/railtie"',
)


class RailsParser(RubyParser):
    name = "ruby-rails"
    priority = 30
    frameworks = ["rails"]

    def claims(self, path: str, source: bytes, parse_timeout_micros: int = 0) -> bool:
        return any(marker in source for marker in _CLAIM_MARKERS)

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("ruby", ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)
        if ctx.capture_statements and not self.is_fixture_file(ctx.path):
            routes = detect_rails_routes(root, ctx.source, ctx.path, record, ctx.resolution_index)
            if routes:
                apply(record, routes, ctx.source, ctx.path)
                record.framework = "rails"
        return record
