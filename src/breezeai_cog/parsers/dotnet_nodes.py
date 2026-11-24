"""Tree nodes of .NET methods and their attributes, shared by the C# and VB framework parsers
(``aspnet_common``, ``wcf_common``, …) — both grammars name them alike.

A framework detector reads an entry point off a method's record (its ``decorators``); these
find the node that record was read from, so the statement sits there: the attribute
(``nodeType: attribute``), or the method itself when there is no attribute to sit on.
"""

from __future__ import annotations

from tree_sitter import Node

from ..schemas import Function
from .dotnet_common import simple_attr_name

#: A method's attributes: C# groups them in ``attribute_list``, VB in ``attribute_block``.
_ATTRIBUTE_GROUPS = ("attribute_list", "attribute_block")


def method_nodes(root: Node) -> dict[int, Node]:
    """Method and constructor declarations by start line — the line their ``Function`` record
    starts at (C# and VB). A line with two declarations maps to none: which one is unknown."""
    return _by_line(root, ("method_declaration", "constructor_declaration"))


def _by_line(root: Node, types: tuple[str, ...]) -> dict[int, Node]:
    out: dict[int, Node] = {}
    clashes: set[int] = set()
    stack = [root]
    while stack:
        n = stack.pop()
        if n.type in types:
            line = n.start_point[0] + 1
            if line in out:
                clashes.add(line)
            out[line] = n
        stack.extend(n.named_children)
    for line in clashes:
        del out[line]
    return out


#: Type declarations a class record is read from: C# and VB.
_TYPE_DECLARATIONS = ("class_declaration", "struct_declaration", "record_declaration",
                      "class_block", "structure_block")


def type_nodes(root: Node) -> dict[int, Node]:
    """Class and struct declarations by start line — the line their ``Class`` record starts at
    (C# and VB), as :func:`method_nodes`."""
    return _by_line(root, _TYPE_DECLARATIONS)


def attribute_nodes(method: Node) -> list[Node]:
    """A method's ``attribute`` nodes in declaration order — the order of its record's
    ``decorators``."""
    return [a for group in method.children if group.type in _ATTRIBUTE_GROUPS
            for a in group.named_children if a.type == "attribute"]


def attribute_at(method: Node | None, fn: Function, index: int) -> Node | None:
    """The node ``fn.decorators[index]`` was read from; the method when its attributes cannot
    be matched one to one; ``None`` without the method."""
    if method is None:
        return None
    attrs = attribute_nodes(method)
    return attrs[index] if len(attrs) == len(fn.decorators) else method


def named_attribute(method: Node | None, fn: Function, name: str) -> Node | None:
    """The node of ``fn``'s first attribute named ``name`` (``[X]`` or ``[XAttribute]``), as
    :func:`attribute_at`; the method when it has none."""
    i = next((i for i, d in enumerate(fn.decorators) if simple_attr_name(d.name) == name), None)
    return attribute_at(method, fn, i) if i is not None else method
