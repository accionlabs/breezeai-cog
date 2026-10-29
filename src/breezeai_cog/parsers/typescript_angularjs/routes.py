"""AngularJS route detection (ngRoute ``$routeProvider``; ui-router lives in ``states.py``).

A route is declared on the injected ``$routeProvider``, usually as a chain, so the whole chain
is one captured statement and every ``.when(…)`` gets a record of its own::

    $routeProvider
      .when('/products', { templateUrl: 'list.html', controller: 'ListCtrl as vm' })  // page
      .when('/old', { redirectTo: '/products' })                                      // navigation
      .otherwise({ redirectTo: '/products' });                                        // navigation

Only the exact name ``$routeProvider`` counts (never ``router.when(…)``), and only a ``when``
whose second argument is an object literal: a variable could hold a redirect as well as a page.
``otherwise`` with a view of its own is a catch-all with no address and is not recorded."""

from __future__ import annotations

from tree_sitter import Node

from ..emits import Emit
from ..typescript.navigation import literal
from .registrations import Env, handler_fields
from .syntax import (
    MethodCall,
    Scan,
    call_emit,
    chain_root,
    cut_query,
    is_identifier,
    object_pairs,
)


def navigation_emit(mc: MethodCall, source: bytes, target: str | None) -> Emit:
    return call_emit(
        mc,
        source,
        {
            "semanticType": "route",
            "routeKind": "navigation",
            "endpoint": target,
        },
    )


def redirect_target(node: Node | None, source: bytes) -> str | None:
    """Where a redirect goes: a literal absolute path, without a query or fragment. A function
    or any other expression has no address that can be read."""
    text = literal(node, source) if node is not None else None
    target = cut_query(text) if text is not None else ""
    return target if target.startswith("/") else None


def _address(node: Node, source: bytes) -> str | None:
    """A route path, when it is one: a literal that starts with ``/``."""
    path = literal(node, source)
    return path if path and path.startswith("/") else None


def _page(mc: MethodCall, view: dict[str, Node], env: Env) -> Emit:
    return call_emit(
        mc,
        env.source,
        {
            "semanticType": "route",
            "routeKind": "page",
            "endpoint": _address(mc.arguments[0], env.source),
            **handler_fields(view, env),
        },
    )


def _when(mc: MethodCall, env: Env) -> Emit | None:
    args = mc.arguments
    if len(args) < 2 or args[1].type != "object":
        return None
    view = object_pairs(args[1], env.source)
    if "redirectTo" in view:
        return navigation_emit(mc, env.source, redirect_target(view["redirectTo"], env.source))
    return _page(mc, view, env)


def _otherwise(mc: MethodCall, source: bytes) -> Emit | None:
    args = mc.arguments
    if not args:
        return None
    if literal(args[0], source) is not None:  # otherwise('/x') is otherwise({ redirectTo: '/x' })
        return navigation_emit(mc, source, redirect_target(args[0], source))
    if args[0].type == "object":
        redirect = object_pairs(args[0], source).get("redirectTo")
        if redirect is not None:
            return navigation_emit(mc, source, redirect_target(redirect, source))
    return None


def ngroute_emits(found: Scan, env: Env) -> list[Emit]:
    emits: list[Emit] = []
    for mc in found.calls:
        name = mc.name(env.source)
        if name not in ("when", "otherwise"):
            continue
        if not is_identifier(chain_root(mc.receiver), env.source, "$routeProvider"):
            continue
        emit = _when(mc, env) if name == "when" else _otherwise(mc, env.source)
        if emit is not None:
            emits.append(emit)
    return emits
