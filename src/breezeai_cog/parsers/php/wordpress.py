"""WordPress hook detection (add_action / add_filter) -> route statements."""

from __future__ import annotations

from tree_sitter import Node

from ...emit import disambiguate, file_id, find_statement_by_span, register_statement_span, statement_id
from ...schemas import Statement
from .owner import owner_id_for_node
from ..treesitter import node_text

_HOOK_FUNCTIONS = frozenset({"add_action", "add_filter"})


def _literal_hook_tag(node: Node, source: bytes) -> str | None:
    """Resolve only a literal hook-tag string; dynamic tags have no static endpoint."""
    if node.type == "argument":
        inner = node.named_children[0] if node.named_children else None
        return _literal_hook_tag(inner, source) if inner is not None else None
    if node.type != "string":
        return None
    content = next((child for child in node.named_children if child.type == "string_content"), None)
    return node_text(content, source) if content is not None else ""


def detect_wordpress_hooks(
    root: Node,
    source: bytes,
    path: str,
    parent_id: str,
    seen_ids: set[str],
) -> list[Statement]:
    """Detect WordPress add_action / add_filter hook registrations."""
    fid = file_id(path)
    out: list[Statement] = []

    def visit(node: Node) -> None:
        if node.type == "function_call_expression":
            fn = node.child_by_field_name("function")
            if fn is not None:
                fn_name = node_text(fn, source)
                if fn_name in _HOOK_FUNCTIONS:
                    args = node.child_by_field_name("arguments")
                    if args is not None and args.named_children:
                        # First arg is the hook tag
                        first_arg = args.named_children[0]
                        hook_tag = _literal_hook_tag(first_arg, source)

                        # Second arg is handler if present
                        handler = None
                        if len(args.named_children) > 1:
                            handler_node = args.named_children[1]
                            handler = node_text(handler_node, source)

                        owner_id = owner_id_for_node(node, source, path)

                        existing = find_statement_by_span(
                            seen_ids, fid, node.start_byte, node.end_byte, node=node
                        )
                        if existing is not None and existing.semanticType is None:
                            existing.semanticType = "route"
                            existing.routeKind = "eventbus_consumer"
                            existing.method = "CONSUMER"
                            existing.endpoint = hook_tag
                            existing.handler = handler
                            existing.framework = "wordpress"
                            existing.parentId = owner_id
                        else:
                            start, col = node.start_point[0] + 1, node.start_point[1]
                            end = node.end_point[0] + 1
                            sid = disambiguate(statement_id(path, start, col), seen_ids)

                            stmt = Statement(
                                id=sid,
                                parentId=owner_id,
                                nodeType=node.type,
                                semanticType="route",
                                routeKind="eventbus_consumer",
                                method="CONSUMER",
                                endpoint=hook_tag,
                                handler=handler,
                                text=node_text(node, source),
                                startLine=start,
                                endLine=end,
                                path=path,
                                framework="wordpress",
                            )
                            register_statement_span(seen_ids, fid, node.start_byte, node.end_byte, stmt)
                            out.append(stmt)
        for child in node.named_children:
            visit(child)

    visit(root)
    return out
