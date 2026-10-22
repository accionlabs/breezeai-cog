"""Lead a Razor Page to its PageModel: ``handlerPath`` / ``handlerLine`` of the ``@model`` class.

A page's ``@page`` record names the ``@model`` class as its handler; the class is declared in the
page's code-behind, ``<page>.cshtml.cs``::

    @* Pages/Orders.cshtml *@
    @page "/orders"
    @model OrdersModel

    // Pages/Orders.cshtml.cs
    namespace Shop.Pages;
    public class OrdersModel : PageModel { … }            // line 2

    → handlerPath "Pages/Orders.cshtml.cs", handlerLine 2

The Razor parser is its own base parser, with no C# index, so its ``handler_locator`` reads that
one file. A simple ``@model`` name is the code-behind's class of that name; a qualified one
(``@model Shop.Pages.OrdersModel``) must also match its namespace. Not declared there (a model
kept in another file, a generic or library type), or declared twice → no location.

The code-behind sits in the page's folder, so the scanner's folder rules treat both alike; only
a ``.repoignore`` *file* pattern that excludes ``*.cshtml.cs`` but keeps the page could leave
``handlerPath`` naming a file the output does not hold.
"""

from __future__ import annotations

from pathlib import Path

from tree_sitter import Node

from ..base import HandlerLocator
from ..treesitter import node_text, parse_source

_NAMESPACES = ("namespace_declaration", "file_scoped_namespace_declaration")
_CLASSES = ("class_declaration", "record_declaration", "struct_declaration")


def class_sites(root: Node, source: bytes) -> dict[str, list[tuple[str, int]]]:
    """Class simple name → every ``(namespace, line)`` it is declared at in this file."""
    found: dict[str, list[tuple[str, int]]] = {}

    def walk(node: Node, ns: str) -> None:
        for child in node.named_children:
            if child.type in _NAMESPACES:
                name = child.child_by_field_name("name")
                inner = node_text(name, source) if name is not None else ""
                inner = f"{ns}.{inner}" if ns and inner else inner or ns
                if child.type == "file_scoped_namespace_declaration":
                    ns = inner  # applies to the rest of the file
                    walk(child, ns)
                else:
                    walk(child, inner)
                continue
            if child.type in _CLASSES:
                name = child.child_by_field_name("name")
                if name is not None:
                    found.setdefault(node_text(name, source), []).append(
                        (ns, child.start_point[0] + 1))
            walk(child, ns)

    walk(root, "")
    return found


def page_model_locator(abs_path: Path, rel: str, timeout_micros: int = 0) -> HandlerLocator | None:
    """The lookup for one ``.cshtml`` page, or None when it has no readable code-behind."""
    code_behind = abs_path.with_name(abs_path.name + ".cs")
    try:
        source = code_behind.read_bytes()
        root = parse_source("csharp", source, timeout_micros).root_node
    except (OSError, ValueError):
        return None
    classes = class_sites(root, source)
    code_behind_rel = rel + ".cs"

    def locate(name: str) -> tuple[str | None, tuple[str, int] | None] | None:
        if "<" in name:
            return None  # a generic type is not the page's own class
        qualifier, _, simple = name.strip().rpartition(".")
        lines = [line for ns, line in classes.get(simple, ()) if not qualifier or ns == qualifier]
        return (name, (code_behind_rel, lines[0])) if len(lines) == 1 else None

    return locate
