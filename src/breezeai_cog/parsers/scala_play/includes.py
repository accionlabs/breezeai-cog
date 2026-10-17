"""Play router includes as a registered fact, ``play.router-includes``.

A ``->`` line in a routes file includes another router under a prefix::

    # conf/routes                              — defines router.Routes (the main router)
    ->   /admin      admin.Routes              # the router compiled from conf/admin.routes
    ->   /v1/posts   v1.post.PostRouter        # a SIRD router class

    # conf/admin.routes                        — defines admin.Routes
    ->   /reports    reports.Routes
    GET  /users      controllers.Admin.users   # served at /admin/users

Each include is wiring (contract §4): a prefix for the bindings of the router it names. A routes
file defines the router named after it (``conf/routes`` → ``router.Routes``, ``conf/admin.routes``
→ ``admin.Routes``), so includes chain: ``reports.Routes`` is served under ``/admin/reports``.

One includer and prefix per router (:func:`~..wiring.resolve_wiring`): a router included twice,
or under a router whose own prefix is unknown, keeps its paths as written. The main router's
prefix is empty. The routes parser reads the fact from its own index, the SIRD detector from the
Scala index; both use :func:`router_prefix`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from ..additive import FactSpec, index_fact, register_fact
from ..index_common import record_distinct
from ..wiring import Wiring, resolve_wiring

FACT = "play.router-includes"

#: The router Play compiles ``conf/routes`` into; it is served at the root.
MAIN_ROUTER = "router.Routes"

#: A binding line (``GET /users controllers.Users.list``).
ROUTE_LINE_RE = re.compile(r"^\s*(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\s+\S+\s+\S")
INCLUDE_RE = re.compile(r"^\s*->\s+(?P<prefix>\S+)\s+(?P<router>[\w.$]+)\s*$")


def is_routes_file(path: str | Path) -> bool:
    """``conf/routes`` or ``conf/<name>.routes``."""
    p = PurePosixPath(Path(path).as_posix())
    return p.parent.name == "conf" and (p.name == "routes" or p.suffix == ".routes")


def router_name(path: str) -> str:
    """The router Play compiles a routes file into: ``conf/admin.routes`` → ``admin.Routes``."""
    name = PurePosixPath(path).name
    return MAIN_ROUTER if name == "routes" else f"{name.removesuffix('.routes')}.Routes"


def join(prefix: str, path: str) -> str:
    """``/admin`` + ``/users`` → ``/admin/users``; ``/admin`` + ``/`` → ``/admin``."""
    base = prefix.rstrip("/")
    if path in ("", "/"):
        return base or "/"
    return base + "/" + path.lstrip("/")


@dataclass(frozen=True)
class RouterIncludes:
    """``prefixes``: router name → the prefix it is served under (``""`` for the main router).
    ``defined``: the routers the repo's routes files define (``conf/admin.routes`` →
    ``admin.Routes``) with at least one binding or include."""

    prefixes: dict[str, str] = field(default_factory=dict)
    defined: frozenset[str] = frozenset()


def served_routers(index: Any | None) -> frozenset[str]:
    """The routers a routes file of the repo defines and whose prefix resolves: an include of
    one is wiring with nothing left to say (its routes carry the prefix)."""
    fact: RouterIncludes | None = index_fact(index, FACT)
    return frozenset(r for r in fact.defined if r in fact.prefixes) if fact is not None \
        else frozenset()


def router_prefix(index: Any | None, router: str) -> str | None:
    """The prefix ``router`` is served under; ``None`` when unknown (keep paths as written)."""
    fact: RouterIncludes | None = index_fact(index, FACT)
    return fact.prefixes.get(router) if fact is not None else None


#: One routes file: its router name, its includes, and whether it has any binding or include.
_FileIncludes = tuple[str, list[Wiring], bool]


def collect_includes(source: bytes, rel: str) -> _FileIncludes | None:
    """The router a routes file defines, and the prefix wiring of its ``->`` lines."""
    if not is_routes_file(rel):
        return None
    wiring = []
    text = source.decode("utf-8", "replace")
    for lineno, line in enumerate(text.splitlines(), start=1):
        m = INCLUDE_RE.match(line)
        if m is not None:
            wiring.append(Wiring((m["router"],), "prefix", m["prefix"], f"{rel}:{lineno}"))
    has_routes = bool(wiring) or any(ROUTE_LINE_RE.match(line) for line in text.splitlines())
    return router_name(rel), wiring, has_routes


def resolve_includes(values: list[_FileIncludes]) -> RouterIncludes | None:
    """Router → full prefix, chaining each include's prefix onto its includer's."""
    includer: dict[str, str | None] = {}
    wiring: list[Wiring] = []
    defined: set[str] = set()
    for router, items, has_routes in values:
        if has_routes:
            defined.add(router)
        for w in items:
            wiring.append(w)
            record_distinct(includer, w.scope[0], router)
    relative = {scope[0]: prefix for scope, prefix in resolve_wiring(wiring).items()
                if includer.get(scope[0]) is not None}

    prefixes: dict[str, str] = {MAIN_ROUTER: ""} if MAIN_ROUTER in defined else {}

    def full(router: str, depth: int = 0) -> str | None:
        if router in prefixes:
            return prefixes[router]
        parent = includer.get(router)
        if router not in relative or parent is None or depth > 32:  # depth: an include cycle
            return None
        base = full(parent, depth + 1)
        if base is None:
            return None
        prefixes[router] = join(base, relative[router])
        return prefixes[router]

    for router in sorted(relative):
        full(router)
    return RouterIncludes(prefixes, frozenset(defined)) if prefixes else None


def _collect(root: object, source: bytes, rel: str, repo_root: Path | None) -> _FileIncludes | None:
    return collect_includes(source, rel)  # routes files only: a Scala source passes no routes file


def _resolve(values: list[_FileIncludes], index: object) -> RouterIncludes | None:
    return resolve_includes(values)


register_fact(FactSpec(
    name=FACT, language="scala", gate=(b"->",), sources=("routes",),
    collect=_collect, resolve=_resolve,
))
