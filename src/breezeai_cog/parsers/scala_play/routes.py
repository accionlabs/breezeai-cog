"""``conf/routes`` extraction — Play's line-based routing DSL, not Scala source. Plain
text, no tree-sitter (see :class:`.parser.PlayRoutesParser`).

Handler resolution is intentionally **not** attempted here: ``PlayRoutesParser`` is its
own base parser (nothing else claims ``conf/routes``), so it gets no ``resolution_index``
from ``_build_indexes`` (keyed by base-parser name). The declared handler text is emitted
verbatim rather than fabricating a resolved link.
"""

from __future__ import annotations

import re

from ...emit import disambiguate, file_id, statement_id
from ...schemas import Statement
from .includes import join

_VERB_RE = re.compile(
    r"^\s*(?P<verb>GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\s+(?P<path>\S+)\s+(?P<handler>.+?)\s*$"
)
_MOUNT_RE = re.compile(r"^\s*->\s+(?P<prefix>\S+)\s+(?P<router>\S+)\s*$")
#: The router of the ``org.webjars:webjars-play`` module: it serves the WebJars' assets.
_WEBJARS_ROUTER = "webjars.Routes"
#: A regex path part, e.g. ``$id<[0-9]+>``.
_REGEX_PART_RE = re.compile(r"\$\w+<[^>]*>")


def _is_skippable(line: str) -> bool:
    s = line.strip()
    # ``+`` modifier lines (e.g. ``+ nocsrf``) are skipped rather than attributed to the
    # next route — a dropped modifier is a known gap, a misattributed one is wrong data.
    return not s or s.startswith(("#", "+"))


def _handler_text(raw: str) -> str:
    """The declared handler with its argument list stripped: ``list(p:Int ?= 0)`` ->
    ``controllers.HomeController.list``."""
    return raw.split("(", 1)[0].strip()


def extract_routes(text: str, path: str, prefix: str | None = None,
                   served: frozenset[str] = frozenset()) -> list[Statement]:
    """The bindings of one routes file. ``prefix``: where its router is served (from the ``->``
    includes, :mod:`.includes`); ``None`` = unknown, paths as written. ``served``: the routers a
    routes file of the repo defines under a resolved prefix — an include of one has no record."""
    fid = file_id(path)
    seen: set[str] = set()
    routes: list[Statement] = []
    for lineno, raw_line in enumerate(text.splitlines(), start=1):  # never byte-concat files
        if _is_skippable(raw_line):
            continue
        m = _VERB_RE.match(raw_line)
        if m is not None:
            routes.append(Statement(
                id=disambiguate(statement_id(path, lineno, 0), seen),
                parentId=fid,
                nodeType="synthetic",
                semanticType="route",
                text=raw_line.strip(),
                method=m["verb"],
                endpoint=join(prefix, m["path"]) if prefix else m["path"],
                handler=_handler_text(m["handler"]),
                framework="play",
                routeKind="route",
                isRegex=bool(_REGEX_PART_RE.search(m["path"])),
                startLine=lineno,
                endLine=lineno,
                path=path,
            ))
            continue
        # ``-> /prefix router.Routes`` includes another router: wiring (contract §4). Its prefix
        # reaches that router's bindings through the index (:mod:`.includes`), so including a
        # routes file of the repo has no record. WebJars' router serves the static assets itself:
        # a GET route, no handler of ours. Any other router (a SIRD class, a generated gRPC
        # router) keeps a ``routeKind="mount"`` record, every verb (the Target Spec's current
        # form).
        m = _MOUNT_RE.match(raw_line)
        if m is not None and m["router"] not in served:
            webjars = m["router"] == _WEBJARS_ROUTER
            routes.append(Statement(
                id=disambiguate(statement_id(path, lineno, 0), seen),
                parentId=fid,
                nodeType="synthetic",
                semanticType="route",
                text=raw_line.strip(),
                method="GET" if webjars else "ANY",
                endpoint=join(prefix, m["prefix"]) if prefix else m["prefix"],
                handler=None if webjars else m["router"],
                framework="play",
                routeKind="route" if webjars else "mount",
                isRegex=False,
                startLine=lineno,
                endLine=lineno,
                path=path,
            ))
    return routes
