"""Plain Java syntax-tree reads shared by the Vert.x detectors (``events.py``, ``legacy.py``):
method invocations, string literals, a local's assigned value, the class of a ``new X(…)``
handler, and the file's imports and package. Nothing here is about Vert.x itself."""

from __future__ import annotations

from tree_sitter import Node

from ..treesitter import node_text


def invocations(root: Node) -> list[Node]:
    """Every ``method_invocation`` under ``root``, in source order."""
    out: list[Node] = []

    def walk(n: Node) -> None:
        if n.type == "method_invocation":
            out.append(n)
        for c in n.named_children:
            walk(c)

    walk(root)
    return out


def java_string(node: Node, source: bytes) -> str | None:
    """A Java ``string_literal`` node → its text value (Java has no string interpolation)."""
    frag = next((c for c in node.named_children if c.type == "string_fragment"), None)
    return node_text(frag, source) if frag is not None else node_text(node, source).strip('"')


def local_value(at: Node, name: str, source: bytes) -> Node | None:
    """The value given to ``name`` in the method enclosing ``at`` — a local initializer or an
    assignment (``impl = new Impl()``, also to a field) — when exactly one is found."""
    method: Node | None = at
    while method is not None and method.type not in ("method_declaration",
                                                     "constructor_declaration"):
        method = method.parent
    found: list[Node] = []

    def walk(n: Node) -> None:
        if n.type == "variable_declarator":
            nm, value = n.child_by_field_name("name"), n.child_by_field_name("value")
            if nm is not None and value is not None and node_text(nm, source) == name:
                found.append(value)
        elif n.type == "assignment_expression":
            left, right = n.child_by_field_name("left"), n.child_by_field_name("right")
            if left is not None and right is not None and \
                    node_text(left, source).removeprefix("this.") == name:
                found.append(right)
        for c in n.named_children:
            walk(c)

    if method is not None:
        walk(method)
    return found[0] if len(found) == 1 else None


def handler_class(arg: Node | None, source: bytes) -> str | None:
    """The class of a handler given as ``new LoadHandler(…)`` (no anonymous body) — passed
    directly or through a local assigned once in the enclosing method — else None."""
    if arg is not None and arg.type == "identifier":
        arg = local_value(arg, node_text(arg, source), source)
    if arg is None or arg.type != "object_creation_expression" \
            or any(c.type == "class_body" for c in arg.named_children):
        return None
    typ = arg.child_by_field_name("type")
    return node_text(typ, source).split("<", 1)[0] if typ is not None else None


def imports_and_package(root: Node, source: bytes) -> tuple[list[str], list[str], str]:
    """This file's single-type imports, on-demand (``.*``) import packages and its package."""
    singles: list[str] = []
    wildcards: list[str] = []
    package = ""
    for node in root.named_children:
        if node.type == "import_declaration":
            text = node_text(node, source).removeprefix("import").strip().rstrip(";").strip()
            if text.startswith("static "):
                continue
            if text.endswith(".*"):
                wildcards.append(text[:-2].strip())
            else:
                singles.append(text)
        elif node.type == "package_declaration":
            nm = next((c for c in node.named_children
                       if c.type in ("scoped_identifier", "identifier")), None)
            package = node_text(nm, source) if nm is not None else ""
    return singles, wildcards, package
