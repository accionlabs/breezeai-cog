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

A name is unique only within one Play project (``<project>/conf/…``): two sub-projects may each
have an ``admin.routes``. So a routes file's router is keyed by ``(project, name)``, and an
include names the router of its own project when that project defines it, else the one other
project that does (a sub-project the app depends on). Defined by two or more other projects → the
include can't be tied to one: none of them takes its prefix, and none takes another include's
either (each may be the one it serves). A router no routes file defines (a SIRD class, a
generated gRPC router) is keyed ``("", name)``.

One includer and prefix per router (:func:`~..wiring.resolve_wiring`): a router included twice,
or under a router whose own prefix is unknown, keeps its paths as written. Each project's main
router's prefix is empty. The routes parser reads the fact from its own index, the SIRD detector
from the Scala index; both use :func:`router_prefix`.
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


def project_of(routes_rel: str) -> str | None:
    """``modules/one/conf/one.routes`` → ``modules/one``; ``conf/routes`` → ``.``."""
    conf = PurePosixPath(routes_rel).parent
    return str(conf.parent) if conf.name == "conf" else None


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


#: ``(project, name)`` of a routes file's router (``conf/routes`` is project ``.``); ``("", name)``
#: for any other router.
RouterKey = tuple[str, str]


@dataclass(frozen=True)
class RouterIncludes:
    """``prefixes``: router → the prefix it is served under (``""`` for a main router).
    ``served``: per project, the router names its ``->`` lines include that a routes file of the
    repo defines (with at least one binding or include) and whose prefix resolves."""

    prefixes: dict[RouterKey, str] = field(default_factory=dict)
    served: dict[str, frozenset[str]] = field(default_factory=dict)


def served_routers(index: Any | None, routes_rel: str) -> frozenset[str]:
    """The routers the ``->`` lines of ``routes_rel`` include that a routes file of the repo
    defines and whose prefix resolves: such an include is wiring with nothing left to say (its
    routes carry the prefix)."""
    fact: RouterIncludes | None = index_fact(index, FACT)
    project = project_of(routes_rel)
    if fact is None or project is None:
        return frozenset()
    return fact.served.get(project, frozenset())


def router_prefix(index: Any | None, router: str, routes_rel: str | None = None) -> str | None:
    """The prefix ``router`` is served under; ``None`` when unknown (keep paths as written).
    ``routes_rel``: the routes file defining ``router``; ``None`` for a router no routes file
    defines (a SIRD class)."""
    fact: RouterIncludes | None = index_fact(index, FACT)
    project = project_of(routes_rel) if routes_rel is not None else ""
    if fact is None or project is None:
        return None
    return fact.prefixes.get((project, router))


#: One routes file: its project, router name, includes, and whether it has any binding or include.
_FileIncludes = tuple[str, str, list[Wiring], bool]


def collect_includes(source: bytes, rel: str) -> _FileIncludes | None:
    """The router a routes file defines, and the prefix wiring of its ``->`` lines (each scoped to
    the router name as written; :func:`resolve_includes` ties it to a project)."""
    project = project_of(rel)
    if not is_routes_file(rel) or project is None:
        return None
    wiring = []
    text = source.decode("utf-8", "replace")
    for lineno, line in enumerate(text.splitlines(), start=1):
        m = INCLUDE_RE.match(line)
        if m is not None:
            wiring.append(Wiring((m["router"],), "prefix", m["prefix"], f"{rel}:{lineno}"))
    has_routes = bool(wiring) or any(ROUTE_LINE_RE.match(line) for line in text.splitlines())
    return project, router_name(rel), wiring, has_routes


def resolve_includes(values: list[_FileIncludes]) -> RouterIncludes | None:
    """Router → full prefix, chaining each include's prefix onto its includer's."""
    defined: dict[str, set[str]] = {}  # router name → the projects whose routes files define it
    for project, router, _items, has_routes in values:
        if has_routes:
            defined.setdefault(router, set()).add(project)

    def target(project: str, name: str) -> RouterKey | None:
        """The router an include in ``project`` names; None when two other projects define it."""
        owners = defined.get(name, set())
        if project in owners:
            return project, name
        if len(owners) == 1:
            return next(iter(owners)), name
        return ("", name) if not owners else None

    includer: dict[RouterKey, RouterKey | None] = {}
    wiring: list[Wiring] = []
    unsure: set[RouterKey] = set()  # a candidate of an include that can't be tied to one router
    served: dict[str, set[str]] = {}
    for project, router, items, _has_routes in values:
        for w in items:
            name = w.scope[0]
            key = target(project, name)
            if key is None:
                unsure.update((owner, name) for owner in defined[name])
                continue
            if key[0]:
                served.setdefault(project, set()).add(name)
            wiring.append(Wiring(key, w.kind, w.value, w.site))
            record_distinct(includer, key, (project, router))
    relative: dict[RouterKey, str] = {
        (s[0], s[1]): prefix for s, prefix in resolve_wiring(wiring).items()
        if includer.get((s[0], s[1])) is not None and (s[0], s[1]) not in unsure}

    prefixes: dict[RouterKey, str] = {
        (project, MAIN_ROUTER): "" for project in defined.get(MAIN_ROUTER, ())}

    def full(router: RouterKey, depth: int = 0) -> str | None:
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

    for key in sorted(relative):
        full(key)
    served_by = {project: frozenset(
        name for name in names if target(project, name) in prefixes)
        for project, names in served.items()}
    return RouterIncludes(prefixes, served_by) if prefixes else None


def _collect(root: object, source: bytes, rel: str, repo_root: Path | None) -> _FileIncludes | None:
    return collect_includes(source, rel)  # routes files only: a Scala source passes no routes file


def _resolve(values: list[_FileIncludes], index: object) -> RouterIncludes | None:
    return resolve_includes(values)


register_fact(FactSpec(
    name=FACT, language="scala", gate=(b"->",), sources=("routes",),
    collect=_collect, resolve=_resolve,
))
