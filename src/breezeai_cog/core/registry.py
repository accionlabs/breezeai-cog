"""In-package parser registry + capability discovery.

Parsers self-register via ``PARSERS`` lists in each ``parsers/<lang>`` subpackage
(:func:`discover_builtin`). A file is parsed by **exactly one** parser, chosen by
:func:`select`: the highest-``priority`` parser whose ``claims(path, source)`` is True
(framework parsers > the base language parser, which is the fallback). There is no
composition — one file, one parser.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from pathlib import Path

from tree_sitter import Node

from ..errors import RegistryError
from ..parsers.additive import all_detectors
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


def parsers_for(path: str | Path, source: bytes | None = None) -> list[LanguageParser]:
    """Return parsers matching the path, optionally using a bounded source prefix."""
    candidates = []
    for parser in _REGISTRY:
        if parser.matches(path):
            candidates.append(parser)
            continue
        matches_source = getattr(parser, "matches_source", None)
        if source is not None and matches_source is not None and matches_source(path, source):
            candidates.append(parser)
    return candidates


def base_parser_for(path: str | Path, source: bytes | None = None) -> LanguageParser | None:
    """The base **language** parser for a file (lowest priority among candidates).
    Used for the extension allow-list, the language label, and the build_index key."""
    candidates = parsers_for(path, source)
    return min(candidates, key=lambda p: p.priority) if candidates else None


def template_extensions() -> frozenset[str]:
    """Every extension any registered parser declares as markup/view, lowercased.

    The scanner drops these before classification unless ``--capture-templates`` is on.
    Read via ``getattr`` because ``template_extensions`` is an optional ``BaseParser``
    capability, not part of the ``LanguageParser`` Protocol."""
    return frozenset(
        ext.lower() for p in _REGISTRY for ext in getattr(p, "template_extensions", ())
    )


def select(
    path: str | Path,
    source: bytes,
    parse_timeout_micros: int = 0,
    parsed_root: Node | None = None,
) -> LanguageParser | None:
    """The single parser that handles this file: the highest-priority candidate whose
    ``claims(path, source, parse_timeout_micros)`` is True (the base language parser is the
    priority-0 fallback)."""
    candidates = parsers_for(path, source)
    claiming = []
    for parser in candidates:
        claim = parser.claims
        parameters = inspect.signature(claim).parameters
        timeout_param = parameters.get("parse_timeout_micros")
        claim_args = [str(path), source]
        claim_kwargs = {}
        if timeout_param is not None:
            if timeout_param.kind is inspect.Parameter.POSITIONAL_ONLY:
                claim_args.append(parse_timeout_micros)
            else:
                claim_kwargs["parse_timeout_micros"] = parse_timeout_micros
        elif any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        ):
            claim_kwargs["parse_timeout_micros"] = parse_timeout_micros
        elif any(
            parameter.kind is inspect.Parameter.VAR_POSITIONAL
            for parameter in parameters.values()
        ):
            claim_args.append(parse_timeout_micros)
        parsed_root_param = parameters.get("parsed_root")
        if (
            parsed_root is not None
            and parsed_root_param is not None
            and parsed_root_param.kind is not inspect.Parameter.POSITIONAL_ONLY
        ):
            claim_kwargs["parsed_root"] = parsed_root
        claims_file = claim(*claim_args, **claim_kwargs)
        if claims_file:
            claiming.append(parser)
    if not claiming:
        return None
    return max(claiming, key=lambda p: p.priority)


def capabilities() -> dict[str, object]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "languages": sorted({p.name for p in _REGISTRY}),
        "extensions": sorted({e for p in _REGISTRY for e in p.extensions}),
        "templateExtensions": sorted(template_extensions()),
        "frameworks": sorted(
            {f for p in _REGISTRY for f in p.frameworks}
            | {f for ds in all_detectors().values() for d in ds for f in d.frameworks}
        ),
        "statementTypes": sorted({s for p in _REGISTRY for s in p.statement_types}),
        "additiveDetectors": {
            lang: [d.name for d in ds] for lang, ds in all_detectors().items()
        },
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
