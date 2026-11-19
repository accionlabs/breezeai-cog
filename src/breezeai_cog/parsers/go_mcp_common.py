"""Shared by the Go MCP packages (``go_mcpgo`` for ``github.com/mark3labs/mcp-go``, ``go_mcp``
for the official SDK ``github.com/modelcontextprotocol/go-sdk``): the tree walk, Go string
literals and the local names an import path is bound to. Each SDK's construct readers stay in
its own package."""

from __future__ import annotations

from tree_sitter import Node

from .treesitter import node_text


def walk(node: Node) -> list[Node]:
    """Every node under ``node``, in source order (an explicit stack: no recursion limit)."""
    out, stack = [], [node]
    while stack:
        n = stack.pop()
        out.append(n)
        stack.extend(reversed(n.named_children))
    return out


def go_string(node: Node | None, source: bytes) -> str | None:
    """The text of a Go string literal (interpreted or raw; escapes kept as written)."""
    if node is None or node.type not in ("interpreted_string_literal", "raw_string_literal"):
        return None
    return node_text(node, source)[1:-1]


def package_names(root: Node, source: bytes, package: str) -> set[str]:
    """Local names the import path ``package`` is bound to in this file: its import alias, else
    its last path element (``…/mcp`` → ``mcp``). A blank (``_``) or dot import binds no name."""
    out: set[str] = set()
    for spec in walk(root):
        if spec.type != "import_spec":
            continue
        if go_string(spec.child_by_field_name("path"), source) != package:
            continue
        alias = spec.child_by_field_name("name")
        name = node_text(alias, source) if alias is not None else package.rsplit("/", 1)[-1]
        if name not in ("_", "."):
            out.add(name)
    return out


_FUNCS = ("function_declaration", "method_declaration", "func_literal")
_BINDINGS = ("short_var_declaration", "var_spec", "assignment_statement", "range_clause")

#: A function's bindings by name: ``(binding node, value)`` (value None when the sides do not
#: line up one to one, or for a parameter).
_Bound = dict[str, list[tuple[Node, Node | None]]]


class HandlerLines:
    """Where a handler passed by name (``mcp.AddTool(s, t, h)``) is declared. Each function's
    bindings are read once, on first use."""

    def __init__(self, source: bytes, functions: dict[str, int]) -> None:
        self.source = source
        #: The file's top-level functions (``mcp_common.function_lines``).
        self.functions = functions
        self._bound: dict[tuple[int, int], _Bound] = {}

    def line(self, ident: Node) -> int | None:
        """The line of the function ``ident`` names: a local bound exactly once in the function
        of the use, before it, to a ``func`` literal (``h := func(…) {…}``) and never assigned
        again gives the literal's line; any other local binding of the name in an enclosing
        function gives none; a name no enclosing function binds gives the top-level
        function's line."""
        name = node_text(ident, self.source)
        fn = _enclosing_func(ident)
        innermost = fn
        while fn is not None:
            binds = self._bindings(fn).get(name)
            if binds:
                if fn != innermost or len(binds) != 1:
                    return None
                at, value = binds[0]
                if (
                    at.type in ("short_var_declaration", "var_spec")
                    and value is not None
                    and value.type == "func_literal"
                    and at.end_byte <= ident.start_byte
                ):
                    return value.start_point[0] + 1
                return None
            fn = _enclosing_func(fn)
        return self.functions.get(name)

    def _bindings(self, fn: Node) -> _Bound:
        key = (fn.start_byte, fn.end_byte)
        found = self._bound.get(key)
        if found is None:
            found = self._bound[key] = _bindings_in(fn, self.source)
        return found


def _enclosing_func(node: Node) -> Node | None:
    cur = node.parent
    while cur is not None and cur.type not in _FUNCS:
        cur = cur.parent
    return cur


def _bindings_in(fn: Node, source: bytes) -> _Bound:
    """Every name ``fn`` (and the functions nested in it) binds: declared, assigned, a ``range``
    target or a parameter (the receiver included)."""
    out: _Bound = {}
    for n in walk(fn):
        if n.type == "parameter_declaration":
            for c in n.children_by_field_name("name"):
                out.setdefault(node_text(c, source), []).append((n, None))
        elif n.type in _BINDINGS:
            left = n.child_by_field_name("left") or n.child_by_field_name("name")
            right = n.child_by_field_name("right") or n.child_by_field_name("value")
            lefts = (
                left.named_children
                if left is not None and left.type == "expression_list"
                else [left]
                if left is not None
                else []
            )
            # `var a, b = …` lists its names as repeated `name` fields.
            if n.type == "var_spec":
                lefts = n.children_by_field_name("name")
            rights = (
                right.named_children
                if right is not None and right.type == "expression_list"
                else [right]
                if right is not None
                else []
            )
            paired = len(lefts) == len(rights)
            for i, target in enumerate(lefts):
                if target.type == "identifier":
                    value = rights[i] if paired else None
                    out.setdefault(node_text(target, source), []).append((n, value))
    return out
