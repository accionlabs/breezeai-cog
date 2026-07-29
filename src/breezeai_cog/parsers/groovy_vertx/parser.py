"""GroovyVertxParser — the Groovy counterpart of :class:`~..java_vertx.parser.VertxParser`.
Selected over the base :class:`~..groovy.parser.GroovyParser` when a Vert.x import is present
(``io.vertx`` 3.x or ``org.vertx`` 2.x — the latter, ``org.vertx.groovy``, is what the P3
webapp-engine API modules use). Reuses ``GroovyParser.extract`` (single parse), then runs
Groovy-AST Vert.x detection; gated by ``--capture-statements`` like all route/event work.

Why this exists: the P3 API modules (employee-api, filter-api, data-api, …) are Vert.x 2.x
written in **Groovy** and register HTTP routes via ``RouteMatcher`` —
``route.get("${prefix}/x", handler)`` or a bare ``rm.with { get("${p}/x", h) }``. The
``.java``-only ``VertxParser`` never claimed these files (wrong extension) and the base
``GroovyParser`` has no framework detection, so every route was emitted as an untyped
statement (``semanticType=null``). This parser closes that gap."""

from __future__ import annotations

from ...schemas import FileRecord
from ..base import ParseContext
from ..groovy.parser import GroovyParser
from ..treesitter import parse_source
from .events import detect_vertx_groovy


class GroovyVertxParser(GroovyParser):
    name = "groovy-vertx"
    priority = 10  # > base GroovyParser (0), so it wins when claims() sniffs a Vert.x import
    frameworks = ["vertx"]

    def claims(self, path: str, source: bytes) -> bool:
        return b"io.vertx" in source or b"org.vertx" in source

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("groovy", ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)  # inherited Groovy extraction (one parse)
        if ctx.capture_statements:  # routes/events are statements — gated by --capture-statements
            if detect_vertx_groovy(root, ctx.source, ctx.path, record):
                record.framework = "vertx"
        return record
