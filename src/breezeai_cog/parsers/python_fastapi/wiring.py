"""FastAPI router wiring across files, as a registered fact on the Python index:
``fastapi.wiring``.

A FastAPI router is usually declared in one file (``router = APIRouter(prefix="/orders")`` in
``app/routers/orders.py``) and mounted in another (``app.include_router(orders.router,
prefix=…)`` in ``app/main.py``). The route address and its guards need both, so each file's
wiring facts (:mod:`.file_facts`) are collected once per repository and composed here:

* every router or app variable gets the prefix its routes are served under, the dependency
  guards it inherits, and the outermost app/router it is reached from;
* an app (``FastAPI()``, at module level or made in a ``create_app()`` function) is a root —
  unless a ``.mount(…)`` anywhere names it or its factory: a sub-application's address is unknown;
* a router included at more than one place is served at each of them: its address is unknown
  (``None``), never its own prefix alone — an address it is not served at;
* a router in an include cycle keeps its own prefix;
* a prefix that exists but cannot be resolved makes the address unknown (``None``);
* a prefix given by a module-level string constant (``API_PREFIX = "/api/v1"``, in this file or
  imported) is resolved; a settings attribute (``settings.API_V1_PREFIX``) is not — the
  environment can override it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tree_sitter import Node

from ..additive import FactSpec, index_fact, register_fact
from ..treesitter import parse_source
from .file_facts import FileFacts, PrefixSpec, file_facts, module_constants

FACT = "fastapi.wiring"

#: (file, variable) of a router or app.
Node_ = tuple[str, str]
#: Resolved wiring of one router variable: (served prefix or None when unknown, inherited
#: dependency guards, the outermost (file, variable) it is reached from).
Wiring = tuple[str | None, tuple[str, ...], Node_]


@dataclass(frozen=True)
class FastapiWiring:
    #: File → {router / app variable → its wiring} for every FastAPI router in the repo.
    routers: dict[str, dict[str, Wiring]] = field(default_factory=dict)
    #: File → {app variable → global middleware names}.
    middleware: dict[str, dict[str, list[str]]] = field(default_factory=dict)


def fastapi_wiring(index: Any | None) -> FastapiWiring | None:
    found: FastapiWiring | None = index_fact(index, FACT)
    return found


def _collect(
    root: Node, source: bytes, rel: str, repo_root: Path | None,
) -> tuple[str, str, FileFacts] | None:
    """(repo root, this file, its wiring facts) — the repo root lets resolve read a constant a
    prefix names from a file without FastAPI wiring."""
    if repo_root is None:
        return None
    return str(repo_root), rel, file_facts(root, source, repo_root, rel)


def _constants(repo_root: Path, rel: str) -> tuple[str, dict[str, str]]:
    try:
        source = (repo_root / rel).read_bytes()
        root = parse_source("python", source).root_node
    except (OSError, ValueError):
        return rel, {}
    return rel, module_constants(root, source)


def _resolve(values: list[tuple[str, str, FileFacts]], index: Any) -> FastapiWiring | None:
    if not values:
        return None
    root = Path(values[0][0])
    facts = {rel: f for _, rel, f in values}

    # Constants referenced by a prefix but declared in a file without FastAPI wiring.
    consts: dict[str, dict[str, str]] = {rel: f.constants for rel, f in facts.items()}
    wanted = sorted({s[1] for f in facts.values()
                     for s in [p for p, _ in f.routers.values()] + [inc[2] for inc in f.includes]
                     if s[0] == "const" and s[1] not in consts})
    consts.update(dict(_constants(root, rel) for rel in wanted))

    def value(spec: PrefixSpec) -> str | None:
        if spec[0] == "lit":
            return spec[1]
        if spec[0] == "const":
            return consts.get(spec[1], {}).get(spec[2])
        return None

    own: dict[Node_, tuple[str | None, tuple[str, ...]]] = {}
    for rel, f in facts.items():
        for var, (spec, deps) in f.routers.items():
            own[(rel, var)] = (value(spec), deps)
    sites: dict[Node_, set[tuple[Node_, str | None, tuple[str, ...]]]] = {}
    for rel, f in facts.items():
        for receiver, child, spec, deps in f.includes:
            if child[0] == "factory":  # the router the factory function creates and returns
                made = facts[child[1]].factories.get(child[2]) if child[1] in facts else None
                if made is None:
                    continue
                target: Node_ = (child[1], made)
            else:
                target = (rel, child[1]) if child[0] == "local" else (child[1], child[2])
            if target in own:
                sites.setdefault(target, set()).add(((rel, receiver), value(spec), deps))

    mounted = set().union(*(f.mounted for f in facts.values()))
    memo: dict[Node_, Wiring] = {}

    def wiring(node: Node_, seen: frozenset[Node_]) -> Wiring:
        if node in memo:
            return memo[node]
        prefix, deps = own.get(node, ("", ()))
        at = sites.get(node, set())
        maker = facts[node[0]].apps.get(node[1])
        if maker is not None:
            # An app is the root of its routes, wherever it is made (``create_app()``) — unless
            # it may be a mounted sub-application: then where it is served is unknown.
            result: Wiring = ((None if (maker or node[1]) in mounted else prefix), deps, node)
        elif not at and node[1] in facts[node[0]].local_routers:
            # Made inside a function and not found included: served wherever the function's
            # result is mounted — unknown, never the bare path.
            result = (None, deps, node)
        elif len(at) > 1:  # served under each include: no single address
            result = (None, deps, node)
        elif not at or node in seen:  # not included anywhere, or a cycle
            result = (prefix, deps, node)
        else:
            (parent, mount, mount_deps), = at
            parent_base, parent_deps, top = wiring(parent, seen | {node})
            guards = parent_deps + mount_deps + deps
            if parent_base is None or mount is None or prefix is None:
                result = (None, guards, top)
            else:
                result = (f"{parent_base}{mount}{prefix}", guards, top)
        if not seen:
            memo[node] = result
        return result

    out: dict[str, dict[str, Wiring]] = {}
    for node in own:
        out.setdefault(node[0], {})[node[1]] = wiring(node, frozenset())
    return FastapiWiring(
        routers=out,
        middleware={rel: f.middleware for rel, f in facts.items() if f.middleware},
    )


register_fact(FactSpec(
    name=FACT, language="python", gate=(b"APIRouter", b"FastAPI", b"include_router", b".mount("),
    collect=_collect, resolve=_resolve,
))
