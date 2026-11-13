"""The C# half of Web Forms navigation: literal ``Response.Redirect`` / ``Server.Transfer`` calls
in a code-behind, read from the C# tree. The records are built by
:func:`~..webforms_common.navigation.detect_navigation`."""

from __future__ import annotations

from tree_sitter import Node

from ..csharp.imports import _positional_args, _string_literal
from ..treesitter import node_text
from ..webforms_common.navigation import REDIRECT_METHODS


def codebehind_redirects(root: Node, source: bytes) -> list[tuple[str, Node]]:
    """(target-string, invocation-node) for each ``Response.Redirect``/``Server.Transfer``
    literal call. AST-based + receiver-checked (not regex) — comments/strings/same-named
    methods on other objects never match. Iterative walk (recursion-safe)."""
    if b"Redirect" not in source and b"Transfer" not in source:  # cheap gate before the walk
        return []
    out: list[tuple[str, Node]] = []
    stack = [root]
    while stack:
        n = stack.pop()
        if n.type == "invocation_expression":
            fn = n.child_by_field_name("function")
            if fn is not None and fn.type == "member_access_expression":
                nm = fn.child_by_field_name("name")
                method = node_text(nm, source) if nm is not None else None
                want_recv = REDIRECT_METHODS.get(method) if method else None
                recv = fn.child_by_field_name("expression")
                recv_tail = node_text(recv, source).rsplit(".", 1)[-1] if recv is not None else ""
                if want_recv is not None and recv_tail == want_recv:
                    args = _positional_args(n)
                    target = _string_literal(args[0], source) if args else None
                    if target is not None:
                        out.append((target, n))
        stack.extend(n.named_children)
    return out
