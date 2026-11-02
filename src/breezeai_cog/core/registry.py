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
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Callable

from tree_sitter import Node

from ..errors import RegistryError
from ..parsers.additive import all_detectors
from ..parsers.base import LanguageParser
from ..schemas import SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class _RegisteredParser:
    parser: LanguageParser
    invoke_claims: Callable[[str, bytes, int, Node | None], bool]


_REGISTRY: list[_RegisteredParser] = []
_REGISTRY_LOCK = RLock()


def _registered_parser(parser: LanguageParser) -> _RegisteredParser:
    """Normalize claims signatures once, when the parser enters the registry.

    Built-in and external parsers may still implement the historical two-argument
    ``claims(path, source)`` form. Signature inspection is registration-time only;
    selection invokes the cached adapter with the complete registry contract.
    """
    claim: Callable[..., bool] = parser.claims
    parameters = inspect.signature(claim).parameters
    timeout_param = parameters.get("parse_timeout_micros")
    if timeout_param is not None:
        if timeout_param.kind is inspect.Parameter.POSITIONAL_ONLY:
            timeout_mode = "positional"
        else:
            timeout_mode = "keyword"
    elif any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
        timeout_mode = "keyword"
    elif any(p.kind is inspect.Parameter.VAR_POSITIONAL for p in parameters.values()):
        timeout_mode = "positional"
    else:
        timeout_mode = "none"

    parsed_root_param = parameters.get("parsed_root")
    pass_parsed_root = (
        parsed_root_param is not None
        and parsed_root_param.kind is not inspect.Parameter.POSITIONAL_ONLY
    )

    def invoke(path: str, source: bytes, timeout: int, parsed_root: Node | None) -> bool:
        args: list[object] = [path, source]
        kwargs: dict[str, object] = {}
        if timeout_mode == "positional":
            args.append(timeout)
        elif timeout_mode == "keyword":
            kwargs["parse_timeout_micros"] = timeout
        if parsed_root is not None and pass_parsed_root:
            kwargs["parsed_root"] = parsed_root
        return claim(*args, **kwargs)

    return _RegisteredParser(parser, invoke)


def register(parser: LanguageParser) -> LanguageParser:
    version = getattr(parser, "schema_version", None)
    if version != SCHEMA_VERSION:
        raise RegistryError(
            f"parser {getattr(parser, 'name', parser)!r} schema_version {version!r} "
            f"!= {SCHEMA_VERSION!r}"
        )
    entry = _registered_parser(parser)
    with _REGISTRY_LOCK:
        _REGISTRY.append(entry)
    return parser


def clear() -> None:
    with _REGISTRY_LOCK:
        _REGISTRY.clear()


def registered() -> list[LanguageParser]:
    with _REGISTRY_LOCK:
        return [entry.parser for entry in _REGISTRY]


def _matching_entries(path: str | Path, source: bytes | None = None) -> list[_RegisteredParser]:
    with _REGISTRY_LOCK:
        entries = list(_REGISTRY)
    candidates = []
    for entry in entries:
        parser = entry.parser
        if parser.matches(path):
            candidates.append(entry)
            continue
        matches_source = getattr(parser, "matches_source", None)
        if source is not None and matches_source is not None and matches_source(path, source):
            candidates.append(entry)
    return candidates


def parsers_for(path: str | Path, source: bytes | None = None) -> list[LanguageParser]:
    """Return parsers matching the path, optionally using a bounded source prefix."""
    return [entry.parser for entry in _matching_entries(path, source)]


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
        ext.lower() for p in registered() for ext in getattr(p, "template_extensions", ())
    )


def select(
    path: str | Path,
    source: bytes,
    parse_timeout_micros: int = 0,
    parsed_root: Node | None = None,
) -> LanguageParser | None:
    """The single parser that handles this file: the highest-priority candidate whose
    ``claims(path, source, parse_timeout_micros, parsed_root)`` is True (the base language
    parser is the priority-0 fallback). Legacy two-argument claim methods are adapted once
    at registration."""
    candidates = _matching_entries(path, source)
    claiming = []
    for entry in candidates:
        if entry.invoke_claims(str(path), source, parse_timeout_micros, parsed_root):
            claiming.append(entry.parser)
    if not claiming:
        return None
    return max(claiming, key=lambda p: p.priority)


def capabilities() -> dict[str, object]:
    parsers = registered()
    return {
        "schemaVersion": SCHEMA_VERSION,
        "languages": sorted({p.name for p in parsers}),
        "extensions": sorted({e for p in parsers for e in p.extensions}),
        "templateExtensions": sorted(template_extensions()),
        "frameworks": sorted(
            {f for p in parsers for f in p.frameworks}
            | {f for ds in all_detectors().values() for d in ds for f in d.frameworks}
        ),
        "statementTypes": sorted({s for p in parsers for s in p.statement_types}),
        "additiveDetectors": {
            lang: [d.name for d in ds] for lang, ds in all_detectors().items()
        },
    }


def discover_builtin() -> None:
    """Register every built-in parser from each ``parsers/<lang>`` subpackage's
    ``PARSERS`` list (idempotent by name; repopulates after :func:`clear`)."""
    from .. import parsers as pkg

    skip = {"base", "treesitter", "detection"}
    existing = {p.name for p in registered()}
    for mod in pkgutil.iter_modules(pkg.__path__):
        if mod.name in skip:
            continue
        module = importlib.import_module(f"{pkg.__name__}.{mod.name}")
        for parser in getattr(module, "PARSERS", []):
            if parser.name not in existing:
                register(parser)
                existing.add(parser.name)
