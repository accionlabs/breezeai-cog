"""Rails controller callbacks — the guards a route inherits from its controller.

A Rails route is declared in ``config/routes.rb`` (``get '/users/:id', to: 'users#show'``) but
its ``before_action :authenticate`` lives in ``UsersController``, another file. The callbacks are
a registered fact on the Ruby index, ``rails.controller-callbacks`` (controller qualified name →
its callbacks; a controller declared in more than one file with different callbacks is dropped —
ambiguous, no guards), looked up by the route's ``controller#action`` handler.

Only what the syntax states is recorded: ``before_action :a, :b`` with literal ``only:`` /
``except:`` action lists, minus ``skip_before_action``. A callback with ``if:`` / ``unless:``
(may not run), a block, or a non-symbol argument names no guard. Callbacks of a parent controller
are included only when that parent is declared in the same file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tree_sitter import Node

from ..additive import FactSpec, index_fact, register_fact
from ..index_common import record_distinct
from ..ruby.models import _iter_classes, _qualified_name, _superclass_name
from ..treesitter import node_text

FACT = "rails.controller-callbacks"

#: One ``before_action`` entry: (guard name, actions it is limited to or None for all,
#: actions it excludes).
Callback = tuple[str, tuple[str, ...] | None, tuple[str, ...]]

_BEFORE = {"before_action", "prepend_before_action"}
_SKIP = {"skip_before_action"}


def _symbols(node: Node | None, source: bytes) -> tuple[str, ...] | None:
    """``:show`` / ``[:show, :edit]`` / ``%i[show edit]`` → action names; None if not literal."""
    if node is None:
        return None
    if node.type in ("simple_symbol", "string"):
        return (node_text(node, source).lstrip(":").strip("'\""),)
    if node.type == "array":
        out: list[str] = []
        for el in node.named_children:
            if el.type in ("simple_symbol", "string", "bare_symbol"):
                out.append(node_text(el, source).lstrip(":").strip("'\""))
            else:
                return None
        return tuple(out)
    return None


def _callbacks(call: Node, source: bytes) -> list[Callback] | None:
    """The entries of one ``before_action`` call, or None when it names no guard reliably."""
    if call.child_by_field_name("block") is not None:
        return None
    args = call.child_by_field_name("arguments")
    names: list[str] = []
    only: tuple[str, ...] | None = None
    exclude: tuple[str, ...] = ()
    for arg in args.named_children if args is not None else []:
        if arg.type == "simple_symbol":
            names.append(node_text(arg, source).lstrip(":"))
        elif arg.type == "pair":
            key_node = arg.child_by_field_name("key")
            if key_node is None:
                return None
            key = node_text(key_node, source).rstrip(":").lstrip(":")
            values = _symbols(arg.child_by_field_name("value"), source)
            if key in ("if", "unless") or values is None:
                return None  # conditional, or an action list we cannot read
            if key == "only":
                only = values
            elif key == "except":
                exclude = values
        else:
            return None
    return [(name, only, exclude) for name in names] or None


def _class_callbacks(cls: Node, source: bytes) -> list[Callback]:
    body = cls.child_by_field_name("body")
    out: list[Callback] = []
    for stmt in body.named_children if body is not None else []:
        if stmt.type not in ("call", "identifier"):
            continue
        method = stmt.child_by_field_name("method") if stmt.type == "call" else stmt
        name = node_text(method, source) if method is not None else ""
        if stmt.type == "call" and stmt.child_by_field_name("receiver") is not None:
            continue
        if name in _BEFORE:
            entries = _callbacks(stmt, source) if stmt.type == "call" else None
            out.extend(entries or [])
        elif name in _SKIP and stmt.type == "call":
            entries = _callbacks(stmt, source) or []
            skipped = {e[0] for e in entries if e[1] is None and not e[2]}
            out = [c for c in out if c[0] not in skipped]
    return out


def controller_callbacks(root: Node, source: bytes) -> list[tuple[str, list[Callback]]]:
    """``(qualified controller name, callbacks)`` for every ``*Controller`` class in one file,
    parent callbacks first when the parent is declared in the same file."""
    if b"before_action" not in source:
        return []
    own: dict[str, list[Callback]] = {}
    parents: dict[str, str] = {}
    for cls in _iter_classes(root):
        name = _qualified_name(cls, source)
        if name is None or not name.endswith("Controller"):
            continue
        own[name] = _class_callbacks(cls, source)
        parent = _superclass_name(cls, source)
        if parent is not None:
            parents[name] = parent

    def chain(name: str, seen: frozenset[str]) -> list[Callback]:
        parent = parents.get(name)
        inherited = chain(parent, seen | {name}) if parent in own and parent not in seen else []
        return inherited + own.get(name, [])

    return [(name, chain(name, frozenset())) for name in own]


def controller_class(handler: str) -> tuple[str, str] | None:
    """``'admin/users#show'`` → (``'Admin::UsersController'``, ``'show'``); None if not that form."""
    if handler.count("#") != 1:
        return None
    path, action = handler.split("#")
    if not path or not action:
        return None
    parts = ["".join(w.capitalize() for w in seg.split("_")) for seg in path.split("/")]
    return "::".join(parts) + "Controller", action


def guards_for(callbacks: list[Callback], action: str) -> list[str]:
    """The guard names that run before ``action``, in declaration order."""
    return [name for name, only, exclude in callbacks
            if (only is None or action in only) and action not in exclude]


def callbacks_of(index: Any | None, controller: str) -> list[Callback] | None:
    """The ``before_action`` callbacks of ``controller``, or None when unknown."""
    found: dict[str, list[Callback]] = index_fact(index, FACT) or {}
    return found.get(controller)


def _collect(
    root: Node, source: bytes, rel: str, repo_root: Path | None,
) -> list[tuple[str, list[Callback]]] | None:
    return controller_callbacks(root, source) or None


def _resolve(
    values: list[list[tuple[str, list[Callback]]]], index: Any,
) -> dict[str, list[Callback]] | None:
    callbacks: dict[str, list[Callback] | None] = {}
    for per_file in values:
        for name, entries in per_file:
            record_distinct(callbacks, name, entries)
    return {k: v for k, v in callbacks.items() if v is not None} or None


register_fact(FactSpec(
    name=FACT, language="ruby", gate=(b"before_action",), collect=_collect, resolve=_resolve,
))
