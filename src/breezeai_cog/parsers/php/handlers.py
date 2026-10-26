"""Lead a PHP entry point to its handler's declaration (contract §5).

Route and hook detectors name the handler as written in the registration
(``UserController@index``, ``InvokableController``, ``my_init``); after parsing,
:meth:`PhpParser.handler_locator` looks the declaration up here:

======================================  ================================================
The record names …                      Looked up as
======================================  ================================================
``Class@method``                        ``php.method-lines`` (:func:`locate_method`), only in
                                        the class's file when the name resolves through this
                                        file's ``use`` imports / namespace or is fully
                                        qualified; otherwise the one repo-wide declaration
``Class`` (invokable controller)        the same, method ``__invoke``
``function`` (WordPress, Slim)          a function declared once in this file
======================================  ================================================

A found handler is written as the method or function name, with ``handlerLine`` /
``handlerPath`` pointing at it (as Rails and Play do); a handler that can't be found keeps
the name as written and no location. A fully qualified class that is not in the repository
(a vendor class) is never matched to a repo class of the same short name.
"""

from __future__ import annotations

from tree_sitter import Node

from ..base import HandlerLocator
from ..handler_sites import locate_method
from ..treesitter import node_text
from .index import PhpIndex

_SCOPES = {"class_declaration", "interface_declaration", "trait_declaration", "enum_declaration"}


def _functions(root: Node, source: bytes) -> dict[str, list[int]]:
    """Global function name (case-folded, as PHP compares them) → declaration lines."""
    out: dict[str, list[int]] = {}

    def visit(node: Node) -> None:
        for child in node.named_children:
            if child.type in _SCOPES:
                continue
            if child.type == "function_definition":
                name = child.child_by_field_name("name")
                if name is not None:
                    out.setdefault(node_text(name, source).casefold(), []).append(
                        child.start_point[0] + 1)
            visit(child)

    visit(root)
    return out


def _class_files(cls: str, index: PhpIndex | None,
                 bindings: dict[str, str]) -> frozenset[str] | None:
    """The file(s) ``cls`` binds to: one file when certain, an empty set when the class is
    named precisely but is not in the repo, None when only the short name is known."""
    if "\\" in cls:
        if index is None:
            return frozenset()
        path = index.fqcn.get(cls) or index.folded_fqcn.get(cls.casefold())
        return frozenset({path}) if path else frozenset()
    bound = bindings.get(cls)
    return frozenset({bound}) if bound else None


def handler_locator(root: Node, source: bytes, path: str, index: PhpIndex | None,
                    bindings: dict[str, str]) -> HandlerLocator:
    functions: dict[str, list[int]] | None = None

    def locate(handler: str) -> tuple[str | None, tuple[str, int] | None] | None:
        nonlocal functions
        written = handler.strip().lstrip("\\")
        cls, _, method = written.rpartition("@")
        if not cls:
            cls, method = written, ""
        within = _class_files(cls, index, bindings)
        site = locate_method(index, "php", cls, method or "__invoke", within=within)
        if site is not None:
            return method or "__invoke", site
        if method or "\\" in cls:
            return None
        if functions is None:
            functions = _functions(root, source)
        lines = functions.get(cls.casefold(), [])
        return (cls, (path, lines[0])) if len(lines) == 1 else None

    return locate
