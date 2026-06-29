"""In-package parser registry + capability discovery (ARCHITECTURE.md §4).

Parsers self-register via ``PARSERS`` lists in each ``parsers/<lang>`` subpackage
(:func:`discover_builtin`). A file is parsed by **exactly one** parser, chosen by
:func:`select`: the highest-``priority`` parser whose ``claims(path, source)`` is True
(framework parsers > the base language parser, which is the fallback). There is no
composition — one file, one parser.
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

from ..errors import RegistryError
from ..parsers.base import LanguageParser
from ..schemas import SCHEMA_VERSION

_REGISTRY: list[LanguageParser] = []


def register(parser: LanguageParser) -> LanguageParser:
    version = getattr(parser, "schema_version", None)
    if version != SCHEMA_VERSION:
        raise RegistryError(
            f"parser {getattr(parser, 'name', parser)!r} schema_version {version!r} "
            f"!= {SCHEMA_VERSION!r}"
        )
    _REGISTRY.append(parser)
    return parser


def clear() -> None:
    _REGISTRY.clear()


def registered() -> list[LanguageParser]:
    return list(_REGISTRY)


def _file_matches(parser: LanguageParser, path: str | Path) -> bool:
    p = Path(path)
    return p.suffix in parser.extensions or p.name in parser.extensions


def parsers_for(path: str | Path) -> list[LanguageParser]:
    """All registered parsers that claim this file by extension/filename."""
    return [p for p in _REGISTRY if _file_matches(p, path)]


def base_parser_for(path: str | Path) -> LanguageParser | None:
    """The base **language** parser for a file (lowest priority among candidates).
    Used for the extension allow-list, the language label, and the build_index key."""
    candidates = parsers_for(path)
    return min(candidates, key=lambda p: p.priority) if candidates else None


def select(path: str | Path, source: bytes) -> LanguageParser | None:
    """The single parser that handles this file: the highest-priority candidate whose
    ``claims(path, source)`` is True (the base language parser is the priority-0 fallback)."""
    candidates = parsers_for(path)
    claiming = [p for p in candidates if p.claims(str(path), source)]
    if not claiming:
        return None
    return max(claiming, key=lambda p: p.priority)


def capabilities() -> dict[str, object]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "languages": sorted({p.name for p in _REGISTRY}),
        "extensions": sorted({e for p in _REGISTRY for e in p.extensions}),
        "frameworks": sorted({f for p in _REGISTRY for f in p.frameworks}),
        "statementTypes": sorted({s for p in _REGISTRY for s in p.statement_types}),
    }


def discover_builtin() -> None:
    """Register every built-in parser from each ``parsers/<lang>`` subpackage's
    ``PARSERS`` list (idempotent by name; repopulates after :func:`clear`)."""
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
