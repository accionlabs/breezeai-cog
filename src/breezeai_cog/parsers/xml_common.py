"""Shared helpers for tree-sitter ``xml``-grammar-based parsers (``xsd``, ``wsdl``).

The ``xml`` grammar is namespace-**unaware** — every element is a generic ``element``/
``STag``/``EmptyElemTag``/``Name``/``Attribute`` node regardless of its ``xs:``/``wsdl:``/
``soap:`` prefix (verified empirically against
``tree_sitter_language_pack.get_language("xml")``). These helpers let a schema/IDL-specific
walker dispatch on the *local* name after stripping the prefix — the same normalization
``ConfigParser._generic_xml`` already applies to a generic ``.xml`` file's root element.
"""

from __future__ import annotations

from tree_sitter import Node

from .treesitter import node_text


def local_name(qualified: str) -> str:
    """Strip a namespace prefix (``xs:element`` → ``element``); a no-op if there is none."""
    return qualified.rsplit(":", 1)[-1]


def document_root(root: Node) -> Node | None:
    """The document's single root ``element`` (skips ``prolog`` and any other structural
    siblings) — the tree's ``<schema>`` / ``<definitions>`` / … top-level tag."""
    return next((c for c in root.named_children if c.type == "element"), None)


def element_tag(node: Node, source: bytes) -> str | None:
    """The local (prefix-stripped) tag name of an ``element`` node, read off its ``STag``
    (open/close form) or ``EmptyElemTag`` (self-closing form). ``None`` if ``node`` isn't a
    tag-bearing element."""
    open_tag = _child(node, "STag") or _child(node, "EmptyElemTag")
    if open_tag is None:
        return None
    name = _child(open_tag, "Name")
    return local_name(node_text(name, source)) if name is not None else None


def element_attr(node: Node, source: bytes, attr: str) -> str | None:
    """The value of ``attr`` (matched by local name, prefix-insensitive) on ``node``'s open
    tag, its delimiting quote pair removed. ``None`` if ``node`` has no such attribute."""
    open_tag = _child(node, "STag") or _child(node, "EmptyElemTag")
    if open_tag is None:
        return None
    for a in open_tag.named_children:
        if a.type != "Attribute":
            continue
        name = _child(a, "Name")
        if name is not None and local_name(node_text(name, source)) == attr:
            value = _child(a, "AttValue")
            # AttValue's text always includes exactly the delimiting `"…"`/`'…'` pair (the
            # grammar requires one), so slicing them off is correct where `.strip("'\"")`
            # was not — that strips *every* leading/trailing quote character, corrupting a
            # value that legitimately starts or ends with one (e.g. `O'Brien'`).
            return node_text(value, source)[1:-1] if value is not None else None
    return None


def child_elements(node: Node) -> list[Node]:
    """Direct child ``element`` nodes of ``node`` (its ``content`` container's named
    ``element`` children). Empty for a self-closing (``EmptyElemTag``-only) element, which
    has no ``content``."""
    content = _child(node, "content")
    return [c for c in content.named_children if c.type == "element"] if content else []


def find_child(node: Node, source: bytes, local: str) -> Node | None:
    """First direct child element of ``node`` whose local (prefix-stripped) tag == ``local``."""
    return next((c for c in child_elements(node) if element_tag(c, source) == local), None)


def _child(node: Node, typ: str) -> Node | None:
    return next((c for c in node.named_children if c.type == typ), None)
