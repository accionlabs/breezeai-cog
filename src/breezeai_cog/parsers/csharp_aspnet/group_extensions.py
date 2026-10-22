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
Anything else — two calls, a call on a parameter, an overload — leaves the address empty. A call
in a file the scan skips is not counted: the one way two callers could look like one.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any

from tree_sitter import Node

from ..additive import FactSpec, index_fact, register_fact
from ..aspnet_common import _Groups, _member_method_name, _receiver
from ..treesitter import node_text

FACT = "aspnet.group-extensions"

# `MapTodos(this RouteGroupBuilder g` — a declaration, not a call.
_DECL_RE = re.compile(rb"\b([A-Za-z_]\w*)\s*\(\s*this\s+(?:[\w.]+\.)?RouteGroupBuilder\b")
# `MapTodos(` / `MapTodos<T>(` — every mention followed by an argument list.
_CALL_RE = re.compile(rb"\b([A-Za-z_]\w*)\s*(?:<[^<>()]*>)?\s*\(")

_Collected = tuple[list[str], dict[str, int], list[tuple[str, str]]]


def _group_calls(root: Node, source: bytes) -> list[tuple[str, str]]:
    """``(method, prefix)`` for each call made on a group with a known prefix in this file."""
    groups = _Groups(root, source, "invocation_expression", "member_access_expression",
                     "function", "name")
    found: list[tuple[str, str]] = []
    stack = [root]
    while stack:
        node = stack.pop()
        stack.extend(node.named_children)
        if node.type != "invocation_expression":
            continue
        func = node.child_by_field_name("function")
        if func is None or func.type != "member_access_expression":
            continue
        recv = _receiver(func, "name")
        is_group = recv is not None and (
            groups._is_group(recv)
            or (recv.type == "identifier" and groups.values.get(node_text(recv, source)) is not None))
        if not is_group:
            continue
        prefix = groups.prefix(recv, frozenset())
        if prefix is not None:
            found.append((_member_method_name(func, source, "name"), prefix))
    return found


def _collect(root: Node, source: bytes, rel: str, repo_root: Path | None) -> _Collected | None:
    declared = [m.group(1).decode() for m in _DECL_RE.finditer(source)]
    mentions = Counter(m.group(1).decode() for m in _CALL_RE.finditer(source))
    calls = mentions - Counter(declared)  # a declaration is not a call
    on_groups = _group_calls(root, source) if b"MapGroup" in source else []
    if not (declared or on_groups):
        return ([], dict(calls), []) if calls else None
    return declared, dict(calls), on_groups


def _resolve(values: list[_Collected], index: Any) -> dict[str, str] | None:
    declared: Counter[str] = Counter()
    calls: Counter[str] = Counter()
    on_groups: dict[str, set[str]] = {}
    for file_declared, file_calls, file_groups in values:
        declared.update(file_declared)
        calls.update(file_calls)
        for method, prefix in file_groups:
            on_groups.setdefault(method, set()).add(prefix)
    out = {method: next(iter(prefixes)) for method, prefixes in sorted(on_groups.items())
           if declared[method] == 1 and calls[method] == 1 and len(prefixes) == 1}
    return out or None


register_fact(FactSpec(name=FACT, language="csharp", gate=(b"(",),
                       collect=_collect, resolve=_resolve))


def group_extension_prefix(index: Any | None, method: str) -> str | None:
    """The prefix of the one group extension method ``method`` is called on, or None."""
    return (index_fact(index, FACT) or {}).get(method)
