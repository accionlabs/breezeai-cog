"""Sinatra framework parser selected over the base Ruby parser."""

from __future__ import annotations

from tree_sitter import Node

from ...schemas import FileRecord
from ..base import ParseContext
from ..ruby.parser import RubyParser
from ..treesitter import node_text, parse_source
from ..ruby.claims import has_require, has_require_relative_helper
from .routes import detect_sinatra_routes


_SINATRA_SUPERCLASSES = {"Sinatra::Base", "Sinatra::Application"}


def _nodes(root: Node):
    yield root
    for child in root.named_children:
        yield from _nodes(child)


def _sinatra_name(node: Node, source: bytes) -> str:
    return node_text(node, source).lstrip(":").strip()


def _has_sinatra_superclass(root: Node, source: bytes) -> bool:
    return any(
        node.type == "class"
        and (superclass := node.child_by_field_name("superclass")) is not None
        and _sinatra_name(superclass, source).removeprefix("<").strip().lstrip(":")
        in _SINATRA_SUPERCLASSES
        for node in _nodes(root)
    )


def _has_sinatra_factory(root: Node, source: bytes) -> bool:
    for node in _nodes(root):
        if node.type != "call" or node.child_by_field_name("method") is None:
            continue
        method = node_text(node.child_by_field_name("method"), source)
        receiver = node.child_by_field_name("receiver")
        args = node.child_by_field_name("arguments")
        first = args.named_children[0] if args is not None and args.named_children else None
        if method == "new" and receiver is not None:
            if _sinatra_name(receiver, source) == "Sinatra":
                return True
            if (
                _sinatra_name(receiver, source) == "Class"
                and first is not None
                and _sinatra_name(first, source) in _SINATRA_SUPERCLASSES
            ):
                return True
    return False


def _has_sinatra_namespace(root: Node, source: bytes) -> bool:
    if root.type == "scope_resolution":
        if node_text(root, source).startswith("Sinatra::"):
            return True
    return any(_has_sinatra_namespace(child, source) for child in root.named_children)


class SinatraParser(RubyParser):
    name = "ruby-sinatra"
    priority = 10
    frameworks = ["sinatra"]

    def claims(
        self,
        path: str,
        source: bytes,
        parse_timeout_micros: int = 0,
        parsed_root: Node | None = None,
    ) -> bool:
        root = parsed_root
        if root is None:
            root = parse_source("ruby", source, parse_timeout_micros).root_node
        direct_sinatra_evidence = (
            has_require(root, source, {"sinatra", "sinatra/base"})
            or _has_sinatra_superclass(root, source)
            or _has_sinatra_factory(root, source)
        )
        helper_sinatra_evidence = (has_require(root, source, {"spec_helper"})
            or has_require_relative_helper(root, source, "spec_helper")) and (
            _has_sinatra_namespace(root, source)
        )
        return direct_sinatra_evidence or helper_sinatra_evidence

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = ctx.parsed_root
        if root is None:
            root = parse_source("ruby", ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)
        if ctx.capture_statements and not self.is_fixture_file(ctx.path):
            if detect_sinatra_routes(root, ctx.source, ctx.path, record):
                record.framework = "sinatra"
        return record
