"""Rails framework parser selected over the base Ruby parser."""

from __future__ import annotations

from tree_sitter import Node

from ...schemas import FileRecord
from ..base import ParseContext
from ..emits import apply
from ..ruby.claims import _nodes, has_call, has_require, has_superclass
from ..ruby.parser import RubyParser
from ..treesitter import node_text, parse_source
from .routes import detect_rails_routes


def _has_engine_routes_draw(root: Node, source: bytes) -> bool:
    for node in _nodes(root):
        if node.type != "call":
            continue
        method = node.child_by_field_name("method")
        receiver = node.child_by_field_name("receiver")
        if method is None or receiver is None or node_text(method, source) != "draw":
            continue
        receiver_text = node_text(receiver, source)
        if receiver_text == "Engine.routes" or receiver_text.endswith("::Engine.routes"):
            return True
    return False


def _has_controller_superclass(root: Node, source: bytes, path: str) -> bool:
    if "controllers" not in path.replace("\\", "/").split("/"):
        return False
    for node in _nodes(root):
        if node.type != "class":
            continue
        superclass = node.child_by_field_name("superclass")
        if superclass is None:
            continue
        name = node_text(superclass, source).lstrip("<").strip().split("::")[-1]
        if name.endswith("Controller"):
            return True
    return False


class RailsParser(RubyParser):
    name = "ruby-rails"
    priority = 30
    frameworks = ["rails"]

    def claims(
        self,
        path: str,
        source: bytes,
        parse_timeout_micros: int = 0,
        parsed_root: Node | None = None,
    ) -> bool:
        if not any(marker in source for marker in (
            b"rails", b"Rails", b"ApplicationController", b"Controller", b"Engine.routes",
        )):
            return False
        root = parsed_root
        if root is None:
            root = parse_source("ruby", source, parse_timeout_micros).root_node
        rails_routes = (
            has_call(root, source, "Rails.application.routes", "draw")
            or _has_engine_routes_draw(root, source)
        )
        rails_controller = (
            has_superclass(root, source, {"ApplicationController"})
            or _has_controller_superclass(root, source, path)
        )
        other_framework = (
            has_require(root, source, {"grape"})
            or has_superclass(root, source, {"Grape::API", "Grape::Endpoint"})
            or has_require(root, source, {"sinatra", "sinatra/base"})
            or has_superclass(root, source, {"Sinatra::Base", "Sinatra::Application"})
        )
        if other_framework and not (rails_routes or rails_controller):
            return False
        return (
            has_require(root, source, {"rails", "action_controller/railtie"})
            or rails_routes
            or rails_controller
        )

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = ctx.parsed_root
        if root is None:
            root = parse_source("ruby", ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)
        if ctx.capture_statements and not self.is_fixture_file(ctx.path):
            routes = detect_rails_routes(root, ctx.source, ctx.path, record, ctx.resolution_index)
            if routes:
                apply(record, routes, ctx.source, ctx.path)
                record.framework = "rails"
        return record
