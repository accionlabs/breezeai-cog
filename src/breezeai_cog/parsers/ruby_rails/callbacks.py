"""Rails controller callbacks — the guards a route inherits from its controller.

A Rails route is declared in ``config/routes.rb`` (``get '/users/:id', to: 'users#show'``) but
its ``before_action :authenticate`` lives in ``UsersController``, another file. The callbacks are
a registered fact on the Ruby index, ``rails.controller-callbacks`` (controller qualified name →
its callbacks; a controller declared in more than one file with different callbacks is dropped —
ambiguous, no guards), looked up by the route's ``controller#action`` handler.

Only what the syntax states is recorded: ``before_action :a, :b`` with literal ``only:`` /
``except:`` action lists. A callback with ``if:`` / ``unless:`` (may not run), a block, or a
non-symbol argument names no guard.

Callbacks are inherited down the ``< Parent`` chain across files. Each controller's callbacks
and skips are recorded in order with its parent constant and lexical scope; the parent is
resolved the way Ruby resolves a constant (innermost enclosing module first, then outward to
the top level) once every file is known. A parent that is not a controller in the repo, or is
declared ambiguously, ends the chain: nothing is inherited past it, never a guessed parent.

``skip_before_action`` removes a callback — inherited ones included — from the actions it names::

    before_action :authenticate_user!
    skip_before_action :authenticate_user!, only: [:create]    # create: no guard; others: guarded

``only:`` removes it from those actions, ``except:`` from every other one. A skip whose actions
cannot be read (``if:`` / ``unless:``, a non-literal list) removes it everywhere, and one whose
callback cannot be read removes every callback: a guard is never kept where it may be skipped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tree_sitter import Node

from ..additive import FactSpec, index_fact, register_fact
from ..index_common import record_distinct
from ..ruby.models import _class_name, _iter_classes, _qualified_name
from ..treesitter import node_text

FACT = "rails.controller-callbacks"

#: One ``before_action`` entry: (guard name, actions it is limited to or None for all,
#: actions it excludes).
Callback = tuple[str, tuple[str, ...] | None, tuple[str, ...]]

#: One ``skip_before_action``: (the callbacks it names, or None when unreadable — all of them;
#: the actions it is limited to, or None for all; the actions it leaves the callback on).
Skip = tuple[tuple[str, ...] | None, tuple[str, ...] | None, tuple[str, ...]]
#: A class body's callbacks and skips, in declaration order: one of the two is set. The flag
#: marks a ``prepend_before_action`` callback, which goes to the front of the chain.
_Op = tuple[Callback | None, Skip | None, bool]

#: ``*_filter`` is the pre-Rails-5.1 spelling of every ``*_action`` callback; same behaviour.
_BEFORE = {"before_action", "prepend_before_action", "append_before_action",
           "before_filter", "prepend_before_filter", "append_before_filter"}
_PREPEND = {"prepend_before_action", "prepend_before_filter"}
_SKIP = {"skip_before_action", "skip_before_filter"}

#: What one controller contributes: (parent constant as written, lexical scopes innermost
#: first, its callbacks and skips in order). Tuples only: two declarations of a class compare
#: equal when they agree.
Declared = tuple[str | None, tuple[str, ...], tuple[_Op, ...]]

_CONSTANT = re.compile(r"(::)?[A-Z]\w*(::[A-Z]\w*)*")

#: Callbacks that certainly reject an anonymous user: Devise's ``authenticate_<scope>!`` (and the
#: ``authenticate(:scope)`` its route blocks are recorded as), a short list of established names,
#: and the ``require_logged_in_<user|moderator|admin>`` family (optionally ``_or_<status>``, as in
#: ``require_logged_in_user_or_400``). The list is exact on purpose: a name that only mentions a
#: user (``load_user``, ``find_user_from_rss_token``) or asks for the opposite
#: (``require_no_user_or_redirect``) is a guard but does not establish that login is required.
#: ``authorize`` is left out: it checks permission (Pundit, CanCan), and a policy can admit guests.
_DEVISE_AUTH = re.compile(r"authenticate_[a-z0-9_]+!|authenticated?\([a-z0-9_:]+\)")
_LOGIN_FAMILY = re.compile(r"require_logged_in_(user|moderator|admin)(_or_\d+)?")
_AUTH_CALLBACKS = frozenset({
    "authenticate", "authenticate_user", "ensure_logged_in", "login_required",
    "require_login", "require_user",
})


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


def _skip(call: Node, source: bytes) -> Skip:
    """One ``skip_before_action`` call (see :data:`Skip`)."""
    args = call.child_by_field_name("arguments")
    names: list[str] = []
    only: tuple[str, ...] | None = None
    keep: tuple[str, ...] = ()
    everywhere = False
    for arg in args.named_children if args is not None else []:
        if arg.type == "simple_symbol":
            names.append(node_text(arg, source).lstrip(":"))
        elif arg.type == "pair":
            key_node = arg.child_by_field_name("key")
            key = node_text(key_node, source).rstrip(":").lstrip(":") if key_node is not None else ""
            values = _symbols(arg.child_by_field_name("value"), source)
            if key in ("if", "unless") or (key in ("only", "except") and values is None):
                everywhere = True  # where it applies is unknown: removed everywhere
            elif key == "only" and values is not None:
                only = values
            elif key == "except" and values is not None:
                keep = values
        else:
            return None, None, ()  # a callback we cannot name: every one may be skipped
    if not names or call.child_by_field_name("block") is not None:
        return None, None, ()
    return (tuple(names), None, ()) if everywhere else (tuple(names), only, keep)


def _apply_skip(callbacks: list[Callback], skip: Skip) -> list[Callback]:
    """``callbacks`` with ``skip`` applied: each one it names runs on fewer actions, or none."""
    names, only, keep = skip
    out: list[Callback] = []
    for cb in callbacks:
        name, cb_only, cb_except = cb
        if names is not None and name not in names:
            out.append(cb)
        elif only is not None:  # skipped for these actions
            out.append((name, cb_only, tuple(dict.fromkeys(cb_except + only))))
        elif keep:  # skipped for every action but these
            left = tuple(a for a in keep
                         if (cb_only is None or a in cb_only) and a not in cb_except)
            if left:
                out.append((name, left, ()))
        # else: skipped everywhere
    return out


def _class_ops(cls: Node, source: bytes) -> list[_Op]:
    """The ``before_action`` callbacks and ``skip_before_action`` skips of a class body."""
    body = cls.child_by_field_name("body")
    out: list[_Op] = []
    for stmt in body.named_children if body is not None else []:
        if stmt.type not in ("call", "identifier"):
            continue
        method = stmt.child_by_field_name("method") if stmt.type == "call" else stmt
        name = node_text(method, source) if method is not None else ""
        if stmt.type == "call" and stmt.child_by_field_name("receiver") is not None:
            continue
        if name in _BEFORE:
            entries = _callbacks(stmt, source) if stmt.type == "call" else None
            out.extend((e, None, name in _PREPEND) for e in entries or [])
        elif name in _SKIP:
            out.append((None, _skip(stmt, source) if stmt.type == "call" else (None, None, ()),
                        False))
    return out


def _run(callbacks: list[Callback], ops: list[_Op]) -> list[Callback]:
    """``callbacks`` (inherited) followed by a class's own callbacks and skips, in order. Declaring
    a callback again replaces the earlier one of that name (Rails moves it to the end, with the
    new ``only:`` / ``except:``), so a guard is never listed twice. A prepended callback goes to
    the front instead, ahead of inherited ones; ``prepend_before_action :a, :b`` prepends ``a``
    then ``b``, so ``b`` runs first."""
    out = list(callbacks)
    for callback, skip, prepend in ops:
        if callback is not None:
            out = [c for c in out if c[0] != callback[0]]
            if prepend:
                out.insert(0, callback)
            else:
                out.append(callback)
        elif skip is not None:
            out = _apply_skip(out, skip)
    return out


def _parent_constant(cls: Node, source: bytes) -> str | None:
    """The superclass as a constant path (``Base``, ``Admin::Base``, ``::Base``), else None
    (``Struct.new(...)``, a variable …)."""
    superclass = cls.child_by_field_name("superclass")
    if superclass is None or not superclass.named_children:
        return None
    text = node_text(superclass.named_children[0], source).strip()
    return text if _CONSTANT.fullmatch(text) else None


def _lexical_scopes(cls: Node, source: bytes) -> tuple[str, ...]:
    """The scopes a constant in ``class X < Parent`` is looked up in, innermost first: each
    enclosing ``module`` / ``class`` adds its name to the one outside it (``module A::B`` is one
    scope, ``A::B``, not ``A`` then ``A::B``). The class's own name is not one of them."""
    names: list[str] = []
    current = cls.parent
    while current is not None:
        if current.type in {"class", "module"}:
            outer = _class_name(current, source)
            if outer is None:
                return ()  # an enclosing scope we cannot name — resolving would be a guess
            names.append(outer)
        current = current.parent
    scopes: list[str] = []
    for name in reversed(names):
        scopes.append(f"{scopes[-1]}::{name}" if scopes else name)
    return tuple(reversed(scopes))


def controller_callbacks(root: Node, source: bytes) -> list[tuple[str, Declared]]:
    """``(qualified controller name, declaration)`` for every ``*Controller`` class in one file."""
    out: list[tuple[str, Declared]] = []
    for cls in _iter_classes(root):
        name = _qualified_name(cls, source)
        if name is None or not name.endswith("Controller"):
            continue
        out.append((name, (_parent_constant(cls, source), _lexical_scopes(cls, source),
                           tuple(_class_ops(cls, source)))))
    return out


def controller_class(handler: str) -> tuple[str, str] | None:
    """``'admin/users#show'`` → (``'Admin::UsersController'``, ``'show'``); None if not that form."""
    if handler.count("#") != 1:
        return None
    path, action = handler.split("#")
    if not path or not action:
        return None
    parts = ["".join(w.capitalize() for w in seg.split("_")) for seg in path.split("/")]
    return "::".join(parts) + "Controller", action


def is_auth_guard(name: str) -> bool:
    """Whether the callback ``name`` certainly rejects anonymous users."""
    return (name in _AUTH_CALLBACKS or _DEVISE_AUTH.fullmatch(name) is not None
            or _LOGIN_FAMILY.fullmatch(name) is not None)


def guards_for(callbacks: list[Callback], action: str) -> list[str]:
    """The guard names that run before ``action``, in declaration order."""
    return [name for name, only, exclude in callbacks
            if (only is None or action in only) and action not in exclude]


@dataclass(frozen=True, slots=True)
class Controllers:
    """The resolved fact: callbacks by exact controller name, plus which declared controller a
    name written in another case means (``Api::V1::UsersController`` for ``API::V1::…``; None
    when two declared controllers differ only by case)."""

    by_name: dict[str, list[Callback]]
    folded: dict[str, str | None]


def callbacks_of(index: Any | None, controller: str) -> list[Callback] | None:
    """The ``before_action`` callbacks of ``controller``, or None when unknown.

    The name a route spells (``api/v1/users`` → ``Api::V1::UsersController``) differs in case
    from a controller declared under a Rails acronym (``API::V1::UsersController``). The exact
    name is tried first; failing that, the one declared controller that matches ignoring case
    — never a choice between several."""
    found: Controllers | None = index_fact(index, FACT)
    if found is None:
        return None
    if controller in found.by_name:
        return found.by_name[controller]
    name = found.folded.get(controller.lower())
    return found.by_name.get(name) if name is not None else None


def _collect(
    root: Node, source: bytes, rel: str, repo_root: Path | None,
) -> list[tuple[str, Declared]] | None:
    return controller_callbacks(root, source) or None


def _parent_of(name: str, declared: Declared, known: dict[str, Declared | None]) -> str | None:
    """The controller ``name`` inherits from, or None when it cannot be established: no parent,
    a parent that is not a controller of this repo, or one declared ambiguously. Lookup is
    Ruby's: the innermost enclosing scope first, then outward to the top level. The class
    being defined is skipped — ``module Admin; class UsersController < UsersController`` names
    the top-level one."""
    parent, scopes, _ = declared
    if parent is None:
        return None
    if parent.startswith("::"):
        candidates = [parent[2:]]
    else:
        candidates = [f"{scope}::{parent}" for scope in scopes] + [parent]
    for candidate in candidates:
        if candidate == name or candidate not in known:
            continue
        return candidate if known[candidate] is not None else None
    return None


def _resolve(values: list[list[tuple[str, Declared]]], index: Any) -> Controllers | None:
    known: dict[str, Declared | None] = {}
    for per_file in values:
        for name, declared in per_file:
            record_distinct(known, name, declared)
    resolved: dict[str, list[Callback]] = {}

    def callbacks(name: str, seen: frozenset[str]) -> list[Callback]:
        if name in resolved:
            return resolved[name]
        declared = known[name]
        assert declared is not None
        parent = _parent_of(name, declared, known)
        inherited: list[Callback] = []
        if parent is not None and parent not in seen:  # a cycle ends the chain too
            inherited = callbacks(parent, seen | {name})
        resolved[name] = _run(inherited, list(declared[2]))
        return resolved[name]

    result = {name: callbacks(name, frozenset()) for name, d in known.items() if d is not None}
    by_name = {k: v for k, v in result.items() if v}
    # Every declared name counts, ambiguous ones too: a case-variant of one is not a unique match.
    folded: dict[str, str | None] = {}
    for name in known:
        record_distinct(folded, name.lower(), name)
    return Controllers(by_name, folded) if by_name else None


register_fact(FactSpec(
    name=FACT, language="ruby", gate=(b"Controller",), collect=_collect, resolve=_resolve,
))
