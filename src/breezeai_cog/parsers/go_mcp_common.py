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


# ── MCP client calls (shared by both SDKs' client readers) ───────────────────

#: ``(value, declared type)`` of one binding of a name; both None when unknown.
Binding = tuple[Node | None, Node | None]

_FUNCTIONS = ("function_declaration", "method_declaration")


def top_function(node: Node) -> Node | None:
    """The package-level function or method enclosing ``node`` (closures included in it)."""
    out, cur = None, node.parent
    while cur is not None:
        if cur.type in _FUNCTIONS:
            out = cur
        cur = cur.parent
    return out


def _side(node: Node | None) -> list[Node]:
    if node is None:
        return []
    return (
        [c for c in node.named_children if c.type != "comment"]
        if (node.type == "expression_list")
        else [node]
    )


def function_bindings(fn: Node, source: bytes) -> dict[str, list[Binding]]:
    """Every binding of a name in ``fn`` and the closures inside it: its parameters (their type),
    ``:=`` / ``var`` declarations and ``=`` / ``op=`` assignments (their value; in ``a, err :=
    f()`` the call is ``a``'s value), ``range`` targets (unknown). A name's writes through a field
    (``req.Params.Name = …``) are not bindings of it."""
    out: dict[str, list[Binding]] = {}
    for n in walk(fn):
        if n.type == "parameter_declaration":
            type_node = n.child_by_field_name("type")
            for name in n.children_by_field_name("name"):
                out.setdefault(node_text(name, source), []).append((None, type_node))
        elif n.type == "var_spec":
            names = [c for c in n.children_by_field_name("name") if c.type == "identifier"]
            values = _side(n.child_by_field_name("value"))
            type_node = n.child_by_field_name("type")
            for i, name in enumerate(names):
                declared = values[i] if len(values) == len(names) else None
                out.setdefault(node_text(name, source), []).append((declared, type_node))
        elif n.type in ("short_var_declaration", "assignment_statement", "range_clause"):
            left = _side(n.child_by_field_name("left"))
            right = _side(n.child_by_field_name("right"))
            op = next((c for c in n.children if not c.is_named and c.type.endswith("=")), None)
            plain = op is not None and op.type in ("=", ":=") and n.type != "range_clause"
            for i, target in enumerate(left):
                if target.type != "identifier":
                    continue
                value: Node | None = None
                if plain and len(right) == len(left):
                    value = right[i]
                elif plain and len(right) == 1 and i == 0:
                    value = right[0]  # `a, err := f()`
                out.setdefault(node_text(target, source), []).append((value, None))
    return out


def selector(node: Node | None, source: bytes) -> tuple[str, str] | None:
    """``pkg.Name`` (a selector, or a qualified type) → ``("pkg", "Name")``."""
    if node is None:
        return None
    if node.type == "selector_expression":
        operand, field = node.child_by_field_name("operand"), node.child_by_field_name("field")
    elif node.type == "qualified_type":
        operand, field = node.child_by_field_name("package"), node.child_by_field_name("name")
    else:
        return None
    if operand is None or field is None:
        return None
    return node_text(operand, source), node_text(field, source)


def call_target(call: Node, source: bytes) -> tuple[str, str] | None:
    """``x.Method(…)`` → ``("x", "Method")``."""
    fn = call.child_by_field_name("function")
    if fn is None or fn.type != "selector_expression":
        return None
    operand = fn.child_by_field_name("operand")
    if operand is None or operand.type != "identifier":
        return None
    return selector(fn, source)


def call_args(call: Node) -> list[Node]:
    args = call.child_by_field_name("arguments")
    return [a for a in (args.named_children if args is not None else []) if a.type != "comment"]


def keyed_fields(literal: Node | None, source: bytes) -> dict[str, Node] | None:
    """``T{Key: value, …}`` (or ``&T{…}``) → ``{"Key": value}``; None when it is not a literal of
    keyed elements."""
    if literal is not None and literal.type == "unary_expression":
        literal = literal.child_by_field_name("operand")
    if literal is None or literal.type != "composite_literal":
        return None
    body = literal.child_by_field_name("body")
    out: dict[str, Node] = {}
    for el in [c for c in (body.named_children if body is not None else []) if c.type != "comment"]:
        if el.type != "keyed_element":
            return None
        parts = [c for c in el.named_children if c.type != "comment"]
        if len(parts) != 2:
            return None
        key, value = (
            p.named_children[0] if p.type == "literal_element" and p.named_children else p
            for p in parts
        )
        out[node_text(key, source)] = value
    return out


def literal_type(literal: Node | None, source: bytes) -> tuple[str, str] | None:
    """The ``pkg.Type`` of ``pkg.Type{…}`` / ``&pkg.Type{…}``."""
    if literal is not None and literal.type == "unary_expression":
        literal = literal.child_by_field_name("operand")
    if literal is None or literal.type != "composite_literal":
        return None
    return selector(literal.child_by_field_name("type"), source)
