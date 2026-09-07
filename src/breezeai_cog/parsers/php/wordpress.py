"""WordPress hook detection (add_action / add_filter) -> route statements."""

from __future__ import annotations

from tree_sitter import Node

from ...emit import disambiguate, statement_id
from ...schemas import Statement
from ..treesitter import node_text

_HOOK_FUNCTIONS = frozenset({"add_action", "add_filter"})


def detect_wordpress_hooks(
    root: Node,
    source: bytes,
    path: str,
    parent_id: str,
    seen_ids: set[str],
) -> list[Statement]:
    """Detect WordPress add_action / add_filter hook registrations."""
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
                        hook_tag = node_text(first_arg, source).strip("'\"")

                        # Second arg is handler if present
                        handler = None
                        if len(args.named_children) > 1:
                            handler_node = args.named_children[1]
                            handler = node_text(handler_node, source)

                        start, col = node.start_point[0] + 1, node.start_point[1]
                        end = node.end_point[0] + 1
                        sid = disambiguate(statement_id(path, start, col), seen_ids)

                        out.append(
                            Statement(
                                id=sid,
                                parentId=parent_id,
                                nodeType=node.type,
                                semanticType="route",
                                routeKind="hook",
                                endpoint=hook_tag,
                                handler=handler,
                                text=node_text(node, source),
                                startLine=start,
                                endLine=end,
                                path=path,
                                framework="wordpress",
                            )
                        )
        for child in node.named_children:
            visit(child)

    visit(root)
    return out
