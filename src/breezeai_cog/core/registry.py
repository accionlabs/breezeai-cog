"""In-package parser registry + capability discovery (ARCHITECTURE.md §4).

Parsers self-register via :func:`register` (called from each
``parsers/<lang>/__init__.py``); :func:`discover_builtin` imports those subpackages
so registration happens. A file is matched to **every** registered parser whose
extension/filename matches and whose ``schema_version`` agrees; multiple matches are
composed by :class:`CompositeParser` (the "passes through all parsers" rule).
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path
from typing import Any

from ..errors import RegistryError
from ..parsers.base import LanguageParser, ParseContext
from ..schemas import SCHEMA_VERSION, FileRecord

_REGISTRY: list[LanguageParser] = []


def register(parser: LanguageParser) -> LanguageParser:
    """Register a parser instance. Rejects mismatched ``schema_version`` (the gate)."""
    version = getattr(parser, "schema_version", None)
    if version != SCHEMA_VERSION:
        raise RegistryError(
            f"parser {getattr(parser, 'name', parser)!r} schema_version {version!r} "
            f"!= {SCHEMA_VERSION!r}"
        )
    _REGISTRY.append(parser)
    return parser


def clear() -> None:
    """Drop all registrations (test isolation)."""
    _REGISTRY.clear()


def registered() -> list[LanguageParser]:
    return list(_REGISTRY)


def _file_matches(parser: LanguageParser, path: str | Path) -> bool:
    p = Path(path)
    exts = parser.extensions
    return p.suffix in exts or p.name in exts  # suffix, or full name (Dockerfile, .env, …)


def parsers_for(path: str | Path) -> list[LanguageParser]:
    """Parsers that claim this file. A matching parser's ``overrides`` supersede
    (skip) the named parsers — so a framework parser can replace the base instead
    of composing with it. Default (no ``overrides``) = compose with everything."""
    matches = [p for p in _REGISTRY if _file_matches(p, path)]
    superseded = {name for p in matches for name in getattr(p, "overrides", ())}
    return [p for p in matches if p.name not in superseded]


def parser_for(path: str | Path) -> LanguageParser | None:
    """The parser for a file: a single match, a :class:`CompositeParser` for several,
    or ``None`` if unclaimed."""
    matches = parsers_for(path)
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    return CompositeParser(matches)


def capabilities() -> dict[str, Any]:
    """Aggregate discovery surface (backs the `capabilities` CLI / API)."""
    return {
        "schemaVersion": SCHEMA_VERSION,
        "languages": sorted({p.name for p in _REGISTRY}),
        "extensions": sorted({e for p in _REGISTRY for e in p.extensions}),
        "frameworks": sorted({f for p in _REGISTRY for f in p.frameworks}),
        "statementTypes": sorted({s for p in _REGISTRY for s in p.statement_types}),
    }


def discover_builtin() -> None:
    """Register every built-in parser. Each ``parsers/<lang>`` subpackage exposes a
    ``PARSERS`` list; registration is idempotent (by name) so this is safe to call
    repeatedly and **repopulates after** :func:`clear` (import side-effects fire only
    once, so we read ``PARSERS`` from the imported module instead)."""
    from .. import parsers as pkg

    skip = {"base", "treesitter", "detection"}
    existing = {p.name for p in _REGISTRY}
    for mod in pkgutil.iter_modules(pkg.__path__):
        if mod.name in skip:
            continue
        module = importlib.import_module(f"{pkg.__name__}.{mod.name}")
        for parser in getattr(module, "PARSERS", []):
            if parser.name not in existing:
                register(parser)
                existing.add(parser.name)


def _union(*lists: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for items in lists:
        for item in items:
            seen.setdefault(item, None)
    return list(seen)


class CompositeParser:
    """Runs several parsers over one file and merges their ``FileRecord``s — the
    union of functions/classes/statements and of the import/export lists."""

    schema_version = SCHEMA_VERSION

    def __init__(self, parsers: list[LanguageParser]) -> None:
        if not parsers:
            raise RegistryError("CompositeParser requires at least one parser")
        self._parsers = parsers
        self.name = "+".join(p.name for p in parsers)
        self.extensions = tuple(_union(*[list(p.extensions) for p in parsers]))
        self.statement_types = _union(*[list(p.statement_types) for p in parsers])
        self.frameworks = _union(*[list(p.frameworks) for p in parsers])

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        base = self._parsers[0].parse_file(ctx)
        for parser in self._parsers[1:]:
            other = parser.parse_file(ctx)
            base.functions.extend(other.functions)
            base.classes.extend(other.classes)
            base.statements.extend(other.statements)
            base.importFiles = _union(base.importFiles, other.importFiles)
            base.externalImports = _union(base.externalImports, other.externalImports)
            base.exports = _union(base.exports, other.exports)
            base.framework = base.framework or other.framework
        return base
