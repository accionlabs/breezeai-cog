"""ui-router states (``$stateProvider``), and the ``angularjs.states`` fact.

A state is a page when it has a ``url``; nested states append their url to their parent's::

    $stateProvider
      .state('app', { abstract: true, url: '/app', templateUrl: 'layout.html' })   // wiring
      .state('app.users', { url: '/users/:id', controller: 'UserCtrl' })           // /app/users/:id
      .state('admin', { parent: 'app', url: '/admin' })                            // /app/admin

The parent is the ``parent`` property, or else the dotted name (``a.b`` is a child of ``a``) —
and it is often declared in another file. While the TypeScript index is built, ``_collect``
reads every file's states and ``_resolve`` keeps each state name declared **once**: a state
declared twice differently, a parent that isn't declared, or a url / parent that isn't a literal
leaves the address **empty** for the state and everything below it (contract §8), never the
child's own url alone.

``abstract`` states, and states without a ``url`` or with a ``redirectTo``, have no record: an
abstract state's url still prefixes its children. A state with several ``views`` has an address
but names a handler only when exactly one view does.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from tree_sitter import Node

from ..additive import DetectContext, FactSpec, index_fact, register_fact
from ..emits import Emit
from ..index_common import record_distinct
from ..treesitter import node_text
from ..typescript.imports import _is_fixture_path
from ..typescript.navigation import literal
from .registrations import Env, handler_fields
from .routes import navigation_emit, redirect_target
from .syntax import (
    MethodCall,
    Scan,
    call_emit,
    chain_root,
    cut_query,
    is_identifier,
    object_pairs,
    scan,
)

FACT = "angularjs.states"


class StateInfo(NamedTuple):
    """What the address of a state depends on."""

    url: str | None
    parent: str | None
    #: The url and the parent are literals: an address can be composed from them.
    resolvable: bool


States = dict[str, StateInfo]


@dataclass(frozen=True, slots=True)
class Declared:
    call: MethodCall
    name: str
    info: StateInfo
    #: The config that names the handler: the state's own, or its only view with a controller.
    view: dict[str, Node]
    #: Has an address of its own: a url, and it is neither abstract nor a redirect.
    routable: bool


def _view(cfg: dict[str, Node], source: bytes) -> dict[str, Node]:
    views = cfg.get("views")
    if views is None:
        return cfg
    if views.type != "object":
        return {}
    entries = list(object_pairs(views, source).values())
    if any(entry.type != "object" for entry in entries):
        return {}
    named = [
        pairs
        for pairs in (object_pairs(entry, source) for entry in entries)
        if "controller" in pairs or "component" in pairs
    ]
    return named[0] if len(named) == 1 else {}


def _declare(mc: MethodCall, source: bytes) -> Declared | None:
    args = mc.arguments
    if not args:
        return None
    if args[0].type == "object":  # .state({ name: 'a', url: '/a' })
        cfg = object_pairs(args[0], source)
        name = literal(cfg["name"], source) if "name" in cfg else None
    elif len(args) > 1 and args[1].type == "object":  # .state('a', { url: '/a' })
        cfg = object_pairs(args[1], source)
        name = literal(args[0], source)
    else:
        return None
    if not name:
        return None
    url = literal(cfg["url"], source) if "url" in cfg else None
    resolvable = "url" not in cfg or url is not None
    if "parent" in cfg:
        parent = literal(cfg["parent"], source)
        resolvable = resolvable and parent is not None
    else:
        parent = name.rpartition(".")[0] or None
    # `abstract: someFlag` could be true: choose the role that records less.
    abstract = "abstract" in cfg and node_text(cfg["abstract"], source) != "false"
    routable = not abstract and "url" in cfg and "redirectTo" not in cfg
    return Declared(mc, name, StateInfo(url, parent, resolvable), _view(cfg, source), routable)


def declared_states(found: Scan, source: bytes) -> list[Declared]:
    out: list[Declared] = []
    for mc in found.calls:
        if mc.name(source) == "state" and is_identifier(
            chain_root(mc.receiver), source, "$stateProvider"
        ):
            declared = _declare(mc, source)
            if declared is not None:
                out.append(declared)
    return out


def address(name: str, states: States) -> str | None:
    """The url of a state: its ancestors' urls and its own, ``^`` starting over; ``None`` when
    any link of the chain can't be read."""
    parts: list[str] = []
    seen: set[str] = set()
    current: str | None = name
    while current is not None:
        info = states.get(current)
        if info is None or not info.resolvable or current in seen:
            return None
        seen.add(current)
        url = cut_query(info.url or "")
        if url.startswith("^"):
            parts.append(url[1:])
            break
        parts.append(url)
        current = info.parent
    url = "".join(reversed(parts)) or "/"
    return url if url.startswith("/") else None


def _states_of(declared: list[Declared]) -> list[tuple[str, StateInfo]]:
    return [(d.name, d.info) for d in declared]


def _collect(
    root: Node, source: bytes, rel: str, repo_root: Path | None
) -> list[tuple[str, StateInfo]] | None:
    if _is_fixture_path(rel):
        return None
    return _states_of(declared_states(scan(root, source), source)) or None


def _resolve(values: list[list[tuple[str, StateInfo]]], index: object) -> States:
    states: dict[str, StateInfo | None] = {}
    for per_file in values:
        for name, info in per_file:
            record_distinct(states, name, info)
    return {name: info for name, info in states.items() if info is not None}


register_fact(
    FactSpec(
        name=FACT,
        language="typescript",
        gate=(b"$stateProvider",),
        collect=_collect,
        resolve=_resolve,
    )
)


def state_lookup(dc: DetectContext, here: list[Declared]) -> States:
    """Every state of the repository, or — for a single file parsed without the repository
    index — this file's own."""
    states = index_fact(dc.ctx.resolution_index, FACT)
    return states if states is not None else _resolve([_states_of(here)], None)


def state_emits(here: list[Declared], found: Scan, env: Env, states: States) -> list[Emit]:
    source = env.source
    emits = [
        call_emit(
            d.call,
            source,
            {
                "semanticType": "route",
                "routeKind": "page",
                "endpoint": address(d.name, states),
                **handler_fields(d.view, env),
            },
        )
        for d in here
        if d.routable
    ]
    for mc in found.calls:  # $urlRouterProvider.otherwise('/home') / otherwise(fn)
        if (
            mc.name(source) == "otherwise"
            and is_identifier(chain_root(mc.receiver), source, "$urlRouterProvider")
            and mc.arguments
        ):
            emits.append(navigation_emit(mc, source, redirect_target(mc.arguments[0], source)))
    return emits
