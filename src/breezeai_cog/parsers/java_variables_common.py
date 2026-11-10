"""Declared types of a Java file's variables, fields and parameters, by name.

Framework detectors read a call by its receiver's declared type (a Spring ``KafkaTemplate``,
a ``TaskScheduler``, a Vert.x ``HttpClient``) rather than by its name.
"""

from __future__ import annotations

from collections.abc import Collection

from tree_sitter import Node

from .treesitter import node_text


def base_type(type_node: Node | None, source: bytes) -> str:
    """A type as written → its simple name: ``java.util.List<String>`` → ``List``."""
    if type_node is None:
        return ""
    return node_text(type_node, source).split("<", 1)[0].rsplit(".", 1)[-1].strip()


def typed_vars(root: Node, source: bytes, types: Collection[str]) -> dict[str, str]:
    """Variable / field / parameter name → declared type, for names declared in the file with a
    type in ``types`` (simple name, generics dropped). A name declared with two different types
    anywhere in the file is dropped: which one a call uses is unsure."""
    out: dict[str, str | None] = {}

    def walk(n: Node) -> None:
        if n.type in ("field_declaration", "local_variable_declaration"):
            typ = base_type(n.child_by_field_name("type"), source)
            for d in n.named_children:
                if d.type == "variable_declarator":
                    nm = d.child_by_field_name("name")
                    if nm is not None:
                        key = node_text(nm, source)
                        out[key] = typ if out.get(key, typ) == typ else None
        elif n.type == "formal_parameter":
            typ = base_type(n.child_by_field_name("type"), source)
            nm = n.child_by_field_name("name")
            if nm is not None:
                key = node_text(nm, source)
                out[key] = typ if out.get(key, typ) == typ else None
        for c in n.named_children:
            walk(c)

    walk(root)
    return {k: v for k, v in out.items() if v is not None and v in types}
