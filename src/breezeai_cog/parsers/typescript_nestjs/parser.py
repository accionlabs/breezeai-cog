"""NestJSParser — a TypeScript framework parser for **decorator-declared routes**.
Selected over the base TypeScriptParser (single parser per file) when ``claims`` finds a
NestJS *or* routing-controllers signature; reuses ``TypeScriptParser.extract`` on the
shared tree, then adds routes. Both frameworks share the same ``@Controller`` + ``@Get``/
``@Post`` decorator grammar, so the same detector serves both — only the ``framework``
label differs. It coexists with other TS framework parsers (Angular) because selection is
per-file by ``claims``."""

from __future__ import annotations

import re

from ...schemas import FileRecord
from ..base import ParseContext
from ..treesitter import parse_source
from ..typescript.parser import TypeScriptParser
from .routes import detect_nest_routes

# Framework-agnostic controller-route signature: a ``@Controller``/``@JsonController``
# class decorator plus at least one HTTP-verb *method* decorator. Matches NestJS,
# routing-controllers, Ts.ED, and hand-rolled/re-exported decorator modules (e.g. a repo's
# local ``./core`` wrapper) — the old Node parser detected all of these as "nestjs-like".
_CONTROLLER_SIG = re.compile(rb"@(?:Json)?Controller\b")
_VERB_SIG = re.compile(rb"@(?:Get|Post|Put|Patch|Delete|Options|Head|All)\b")


class NestJSParser(TypeScriptParser):
    name = "typescript-nestjs"
    # Above ExpressParser (priority 10): NestJS / routing-controllers are built on Express
    # and their controllers routinely ``import { Request } from 'express'``, which the
    # Express parser also claims. A decorator-controller signature is decisive, so this
    # must win the selection.
    priority = 20
    frameworks = ["nestjs", "routing-controllers", "nestjs-like"]

    def claims(self, path: str, source: bytes) -> bool:
        if b"@nestjs/" in source or b"routing-controllers" in source:
            return True
        # Custom / re-exported decorator modules: detect the pattern itself (a @Controller
        # class with HTTP-verb method decorators), not the import source.
        return bool(_CONTROLLER_SIG.search(source) and _VERB_SIG.search(source))

    @staticmethod
    def _framework(source: bytes) -> str:
        if b"@nestjs/" in source:
            return "nestjs"
        if b"routing-controllers" in source:
            return "routing-controllers"
        return "nestjs-like"  # custom / re-exported decorators (matches the legacy label)

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        grammar = "tsx" if ctx.path.endswith((".tsx", ".jsx")) else "typescript"
        root = parse_source(grammar, ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)  # inherited base extraction (one parse)
        if ctx.capture_statements and not self.is_fixture_file(ctx.path):  # gated by --capture-statements; skip fixtures (R4)
            framework = self._framework(ctx.source)
            routes = detect_nest_routes(
                root, ctx.source, ctx.path,
                seen_ids={s.id for s in record.statements}, framework=framework,
            )
            if routes:
                record.statements.extend(routes)
                record.framework = framework
        return record
