"""Route groups handed to an extension method: a registered fact on the C# index,
``aspnet.group-extensions``.

Minimal-API endpoints are often mapped in an extension method on a group made in another file::

    // Program.cs
    app.MapGroup("/todos").MapTodos();

    // TodoEndpoints.cs
    public static RouteGroupBuilder MapTodos(this RouteGroupBuilder g) {
        g.MapGet("/", GetAll);                 // → GET /todos
        return g;
    }

Inside ``MapTodos`` the group's prefix is the caller's. The fact maps the method to it only when
nothing else can reach the method: exactly one ``MapTodos(this RouteGroupBuilder …)`` declaration
in the repo, exactly one call of that name anywhere (any receiver, ``Endpoints.MapTodos(g)``
included, counted in every C# file), and that call made on a ``MapGroup`` whose prefix is known.
The call may be written either way C# allows: ``grp.MapTodos()``, or the static form
``TodoEndpoints.MapTodos(grp)`` / bare ``MapTodos(grp)``, where the group is the first argument.
A static call counts only through the declaring class (``other.MapTodos(grp)`` makes ``other``
the group, not ``grp``). Anything else — two calls, a call on a parameter, an overload — leaves
the address empty. A call in a file the scan skips is not counted: the one way two callers
could look like one.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any

from tree_sitter import Node

from ..additive import FactSpec, index_fact, register_fact
from ..aspnet_common import _Groups, _member_method_name, _receiver, extension_of
from ..treesitter import node_text

FACT = "aspnet.group-extensions"

# `MapTodos(this RouteGroupBuilder g` — a declaration, not a call.
_DECL_RE = re.compile(rb"\b([A-Za-z_]\w*)\s*\(\s*this\s+(?:[\w.]+\.)?RouteGroupBuilder\b")
# `MapTodos(` / `MapTodos<T>(` — every mention followed by an argument list.
_CALL_RE = re.compile(rb"\b([A-Za-z_]\w*)\s*(?:<[^<>()]*>)?\s*\(")

# (method, declaring class) per declaration; method → call count; (method, prefix, class named
# by a static call or None) per call on a known group.
_Collected = tuple[list[tuple[str, str | None]], dict[str, int], list[tuple[str, str, str | None]]]


def _last_segment(expr: Node | None, source: bytes) -> str | None:
    """``TodoEndpoints`` for ``Api.TodoEndpoints`` / ``global::TodoEndpoints``."""
    return re.split(r"[.:]", node_text(expr, source))[-1].strip() if expr is not None else None


def _first_arg(call: Node) -> Node | None:
    """The first argument of a call when it is positional (the ``this`` argument of an extension
    method called in its static form), else None."""
    args = call.child_by_field_name("arguments")
    first = next((a for a in args.named_children if a.type == "argument"), None) \
        if args is not None else None
    if first is None or first.child_by_field_name("name") is not None or not first.named_children:
        return None
    return first.named_children[-1]


def _group_calls(root: Node, source: bytes) -> list[tuple[str, str, str | None]]:
    """``(method, prefix, class)`` for each call made on a group with a known prefix in this
    file: ``grp.MapX()`` and bare ``MapX(grp)`` (class None), or ``Cls.MapX(grp)`` (class
    ``Cls``, checked against the declaring class on resolve)."""
    groups = _Groups(root, source, "invocation_expression", "member_access_expression",
                     "function", "name")

    def is_group(expr: Node | None) -> bool:
        return expr is not None and (
            groups._is_group(expr)
            or (expr.type == "identifier" and groups.values.get(node_text(expr, source)) is not None))

    found: list[tuple[str, str, str | None]] = []
    stack = [root]
    while stack:
        node = stack.pop()
        stack.extend(node.named_children)
        if node.type != "invocation_expression":
            continue
        func = node.child_by_field_name("function")
        if func is None:
            continue
        if func.type == "member_access_expression":
            method, recv = _member_method_name(func, source, "name"), _receiver(func, "name")
            group, cls = (recv, None) if is_group(recv) \
                else (_first_arg(node), _last_segment(recv, source))
        elif func.type in ("identifier", "generic_name"):
            ident = func if func.type == "identifier" else \
                next((c for c in func.named_children if c.type == "identifier"), None)
            if ident is None:
                continue
            method, group, cls = node_text(ident, source), _first_arg(node), None
        else:
            continue
        if not is_group(group):
            continue
        prefix = groups.prefix(group, frozenset())
        if prefix is not None:
            found.append((method, prefix, cls))
    return found


_TYPE_DECLARATIONS = frozenset({"class_declaration", "struct_declaration", "record_declaration"})


def _declarations(root: Node, source: bytes) -> list[tuple[str, str | None]]:
    """``(method, declaring class)`` for each ``MapX(this RouteGroupBuilder …)`` in this file."""
    found: list[tuple[str, str | None]] = []
    stack = [root]
    while stack:
        node = stack.pop()
        stack.extend(node.named_children)
        if node.type != "method_declaration":
            continue
        name = node.child_by_field_name("name")
        params = node.child_by_field_name("parameters")
        first = next((c for c in params.named_children if c.type == "parameter"), None) \
            if params is not None else None
        if name is None or first is None:
            continue
        pname = first.child_by_field_name("name")
        if pname is None or extension_of(pname, node_text(pname, source), source) is None:
            continue
        cls = node.parent
        while cls is not None and cls.type not in _TYPE_DECLARATIONS:
            cls = cls.parent
        cls_name = cls.child_by_field_name("name") if cls is not None else None
        found.append((node_text(name, source),
                      node_text(cls_name, source) if cls_name is not None else None))
    return found


def _collect(root: Node, source: bytes, rel: str, repo_root: Path | None) -> _Collected | None:
    names = [m.group(1).decode() for m in _DECL_RE.finditer(source)]
    mentions = Counter(m.group(1).decode() for m in _CALL_RE.finditer(source))
    calls = mentions - Counter(names)  # a declaration is not a call
    # The tree is only walked for the declaring class when the text has a declaration; a name
    # the regex saw but the tree does not confirm still counts, with an unknown class.
    declared = _declarations(root, source) if names else []
    found = Counter(n for n, _ in declared)
    declared += [(n, None) for n in (Counter(names) - found).elements()]
    on_groups = _group_calls(root, source) if b"MapGroup" in source else []
    if not (declared or on_groups):
        return ([], dict(calls), []) if calls else None
    return declared, dict(calls), on_groups


def _resolve(values: list[_Collected], index: Any) -> dict[str, str] | None:
    declared: Counter[str] = Counter()
    declaring: dict[str, str | None] = {}
    calls: Counter[str] = Counter()
    for file_declared, file_calls, _ in values:
        for method, cls in file_declared:
            declared[method] += 1
            declaring[method] = cls
        calls.update(file_calls)
    on_groups: dict[str, set[str | None]] = {}
    for _, _, file_groups in values:
        for method, prefix, cls in file_groups:
            # A static call must name the declaring class: `other.MapX(grp)` is made on `other`.
            ok = cls is None or (declaring.get(method) is not None and cls == declaring[method])
            on_groups.setdefault(method, set()).add(prefix if ok else None)
    out = {method: p for method, prefixes in sorted(on_groups.items())
           if declared[method] == 1 and calls[method] == 1 and len(prefixes) == 1
           and (p := next(iter(prefixes))) is not None}
    return out or None


register_fact(FactSpec(name=FACT, language="csharp", gate=(b"(",),
                       collect=_collect, resolve=_resolve))


def group_extension_prefix(index: Any | None, method: str) -> str | None:
    """The prefix of the one group extension method ``method`` is called on, or None."""
    return (index_fact(index, FACT) or {}).get(method)
