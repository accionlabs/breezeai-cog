"""Rails framework parser selected over the base Ruby parser."""

from __future__ import annotations

import re

from ...schemas import FileRecord
from ..base import ParseContext
from ..emits import apply
from ..ruby.parser import RubyParser
from ..treesitter import parse_source
from .routes import detect_rails_routes

_RAILS_REQUIRE = re.compile(
    rb"""(?m)^\s*require(?:_relative)?\s*(?:\(\s*)?['"](?:rails|action_controller/railtie)['"]"""
)
_RAILS_ROUTES = re.compile(
    rb"(?m)^\s*Rails\s*\.\s*application\s*\.\s*routes\s*\.\s*draw\b"
    rb"|^\s*[A-Z]\w*::Engine\s*\.\s*routes\s*\.\s*draw\b"
)
_RAILS_CONTROLLER = re.compile(
    rb"(?m)^\s*class\s+[A-Za-z_]\w*(?:::[A-Za-z_]\w*)*\s*<\s*"
    rb"(?:::)?(?:ApplicationController|ActionController::Base)\b"
)
_OTHER_FRAMEWORK = re.compile(
    rb"""(?im)^\s*require(?:_relative)?\s*(?:\(\s*)?['"](?:grape|sinatra(?:/base)?)['"]"""
    rb"|^\s*class\s+[A-Za-z_]\w*(?:::[A-Za-z_]\w*)*\s*<\s*"
    rb"(?:::)?(?:Grape::(?:API|Endpoint)|Sinatra::(?:Base|Application))\b"
)


class RailsParser(RubyParser):
    name = "ruby-rails"
    priority = 30
    frameworks = ["rails"]

    def claims(self, path: str, source: bytes) -> bool:
        if not any(
            marker in source.lower()
            for marker in (b"rails", b"engine", b"controller", b"actioncontroller")
        ):
            return False
        rails_routes = _RAILS_ROUTES.search(source) is not None
        normalized_path = path.replace("\\", "/").split("/")
        rails_controller = (
            _RAILS_CONTROLLER.search(source) is not None
            or (
                "controllers" in normalized_path
                and re.search(
                    rb"(?m)^\s*class\s+[A-Za-z_]\w*(?:::[A-Za-z_]\w*)*\s*<\s*"
                    rb"(?:::)?(?:[A-Za-z_]\w*::)*[A-Za-z_]\w*Controller\b",
                    source,
                )
                is not None
            )
        )
        if _OTHER_FRAMEWORK.search(source) and not (rails_routes or rails_controller):
            return False
        return bool(_RAILS_REQUIRE.search(source) or rails_routes or rails_controller)

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("ruby", ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)
        if ctx.capture_statements and not self.is_fixture_file(ctx.path):
            routes = detect_rails_routes(root, ctx.source, ctx.path, record, ctx.resolution_index)
            if routes:
                apply(record, routes, ctx.source, ctx.path)
                record.framework = "rails"
        return record
