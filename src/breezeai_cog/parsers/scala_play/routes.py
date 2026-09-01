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

_VERB_RE = re.compile(
    r"^\s*(?P<verb>GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\s+(?P<path>\S+)\s+(?P<handler>.+?)\s*$"
)
_MOUNT_RE = re.compile(r"^\s*->\s+(?P<prefix>\S+)\s+(?P<router>\S+)\s*$")
#: A regex path part, e.g. ``$id<[0-9]+>``.
_REGEX_PART_RE = re.compile(r"\$\w+<[^>]*>")


def _is_skippable(line: str) -> bool:
    s = line.strip()
    # ``+`` modifier lines (e.g. ``+ nocsrf``) are skipped rather than attributed to the
    # next route — a dropped modifier is a known gap, a misattributed one is wrong data.
    return not s or s.startswith("#") or s.startswith("+")


def _handler_text(raw: str) -> str:
    """The declared handler with its argument list stripped: ``list(p:Int ?= 0)`` ->
    ``controllers.HomeController.list``."""
    return raw.split("(", 1)[0].strip()


def extract_routes(text: str, path: str) -> list[Statement]:
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
                endpoint=m["path"],
                handler=_handler_text(m["handler"]),
                framework="play",
                routeKind="route",
                isRegex=bool(_REGEX_PART_RE.search(m["path"])),
                startLine=lineno,
                endLine=lineno,
                path=path,
            ))
            continue
        m = _MOUNT_RE.match(raw_line)
        if m is not None:
            routes.append(Statement(
                id=disambiguate(statement_id(path, lineno, 0), seen),
                parentId=fid,
                nodeType="synthetic",
                semanticType="route",
                text=raw_line.strip(),
                method=None,
                endpoint=m["prefix"],
                handler=m["router"],
                framework="play",
                routeKind="mount",
                isRegex=False,
                startLine=lineno,
                endLine=lineno,
                path=path,
            ))
    return routes
