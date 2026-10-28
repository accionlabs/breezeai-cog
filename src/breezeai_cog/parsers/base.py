"""The parser plugin contract.

A ``LanguageParser`` declares capability metadata (for discovery + the
schema-version composition gate) and a ``parse_file`` that turns one file into a
:class:`~breezeai_cog.schemas.FileRecord`. ``build_index`` is an **optional**
repo-level pre-pass (Java FQCN, TS aliases, Angular mounts) — parsers that need no
cross-file context simply omit it; the pipeline calls it via ``getattr``.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence, runtime_checkable

from ..schemas import SCHEMA_VERSION, FileRecord


#: A language's handler lookup: the handler name as written → ``(handler, (file, line))`` — the
#: name to keep (a path-shaped name becomes the declared one, or None when unknown) and where it
#: is declared (None: not found) — or None to leave the record as it is.
HandlerLocator = Callable[[str], tuple[str | None, tuple[str, int] | None] | None]


def _read_sibling_lines(obj: object, filename: str) -> list[str]:
    """Read a text file (e.g. ``ignore.txt``) shipped in the parser's subpackage."""
    package = type(obj).__module__.rpartition(".")[0]
    if not package:
        return []
    try:
        return resources.files(package).joinpath(filename).read_text("utf-8").splitlines()
    except (FileNotFoundError, ModuleNotFoundError, OSError, NotADirectoryError):
        return []


#: Global route-only fixture markers — filename infixes shared across ecosystems whose
#: files are parsed for structure but never treated as a route source. Layer 1; a
#: language/framework parser extends it via ``fixture_markers`` (layer 2). Full test-file
#: *exclusion* (``*.test.*``/``*.spec.*``/``test_*.py``…) lives in ``default_ignores.txt``;
#: this list is the narrower route-only guard for files that are still captured.
_GLOBAL_FIXTURE_MARKERS: tuple[str, ...] = (".test.", ".spec.")

#: Directory names (matched as whole path segments, not substrings) whose files are mock /
#: fixture data — parsed for structure but never a real route source. A route config living
#: under one of these is a dev stand-in (e.g. a mock backend that serves the app's dynamic
#: routes), not the wired router, so route/entry-point emitters skip it. Segment-exact so
#: ``mockups/`` (a real word) does not match ``mock``.
_GLOBAL_FIXTURE_DIRS: frozenset[str] = frozenset(
    {"mock", "mocks", "__mocks__", "fixtures", "__fixtures__"}
)


@dataclass(frozen=True, slots=True)
class ParseContext:
    """Everything a parser needs for one file (built per file by the pipeline)."""

    path: str  # repo-relative
    abs_path: Path
    source: bytes  # read by the worker (cached per-process); only the path crosses the boundary
    repo_root: Path
    capture_statements: bool = False
    statement_text_limit: int = 8000  # max captured code statement text/body length
    parse_timeout_micros: int = 0  # cross-platform tree-sitter timeout (0 = none)
    resolution_index: Any | None = None  # result of the language's build_index, or None


@runtime_checkable
class LanguageParser(Protocol):
    """Structural contract a parser must satisfy. ``build_index`` is optional and so
    is intentionally absent here — see :class:`BaseParser` for the default."""

    name: str
    extensions: tuple[str, ...]
    schema_version: str
    statement_types: list[str]
    frameworks: list[str]

    def parse_file(self, ctx: ParseContext) -> FileRecord: ...

    def link_handlers(self, record: FileRecord, ctx: ParseContext) -> None: ...


class BaseParser:
    """Convenience base: capability-metadata defaults + a no-op ``build_index``.

    Subclasses set ``name``/``extensions``/``statement_types``/``frameworks`` and
    implement ``parse_file``.
    """

    name: str = ""
    extensions: tuple[str, ...] = ()
    schema_version: str = SCHEMA_VERSION
    statement_types: list[str] = []
    frameworks: list[str] = []
    #: Selection priority. A file is parsed by exactly ONE parser — the highest-priority
    #: one whose ``claims`` is True (framework parsers > base; base = 0, the fallback).
    priority: int = 0
    #: The subset of ``extensions`` that are markup/view **templates** rather than code.
    #: Skipped at scan time unless ``--capture-templates`` is given. Declared per parser
    #: because a parser may own both kinds (WebForms owns ``.cs`` + ``.aspx``; Vue owns
    #: ``.ts``/``.js`` + ``.vue``) and only the markup half is gated. Optional, like
    #: ``priority``/``build_index`` — deliberately absent from the Protocol above.
    template_extensions: tuple[str, ...] = ()

    def matches(self, path: str | Path) -> bool:
        """Whether this parser handles ``path`` by name/extension (the candidacy gate,
        before ``claims``). Default: suffix or exact filename in ``extensions``. Override
        for filename patterns (e.g. the config parser's ``Dockerfile.*`` / ``.env.*``).

        A template extension matches in any case (``Default.ASPX``, ``Site.Master``, common in
        older ASP.NET trees), as the template gate does; code extensions stay case-sensitive
        (``.TS`` is also an MPEG video)."""
        p = Path(path)
        suffix = p.suffix
        return (suffix in self.extensions or p.name in self.extensions
                or (bool(self.template_extensions) and suffix.lower() in self.template_extensions))

    def claims(self, path: str, source: bytes) -> bool:
        """Whether this parser should handle ``path``. The base language parser claims
        everything of its extension (fallback); framework parsers override this to sniff
        their framework's signature in ``source`` (e.g. ``b"@nestjs/" in source``)."""
        return True

    def build_index(
        self, repo_root: Path, files: Sequence[Path], jobs: int = 1
    ) -> Any | None:  # optional pre-pass
        return None

    def link_handlers(self, record: FileRecord, ctx: ParseContext) -> None:
        """After ``parse_file``: lead each entry point that names its handler but not where it is
        declared to that declaration (``handlerLine``, and ``handlerPath`` for another file).

        The loop and its rules are the same for every language: only unlocated records, the
        "own file → no ``handlerPath``" rule (:func:`~.handler_sites.handler_location`), and
        honest-null — a name the language can't find is left as it is. A language supplies only
        the lookup, :meth:`handler_locator`, which is asked for only when a record needs it."""
        if not ctx.capture_statements:
            return
        from .handler_sites import handler_location, unlocated  # handler_sites imports this module

        pending = [s for s in record.statements if unlocated(s)]
        if not pending:
            return
        locate = self.handler_locator(ctx)
        if locate is None:
            return
        for s in pending:
            found = locate(s.handler or "")
            if found is None:
                continue
            s.handler, site = found
            s.handlerLine, s.handlerPath = handler_location(site, ctx.path)

    def handler_locator(self, ctx: ParseContext) -> HandlerLocator | None:
        """This file's handler lookup for :meth:`link_handlers`, or None when the language
        locates nothing after parsing (the default). Detectors that know the class look their
        handler up inline instead (:mod:`.handler_sites`)."""
        return None

    def ignore_patterns(self) -> list[str]:
        """Per-language ignore defaults (layer 2) — from sibling ``ignore.txt``."""
        return _read_sibling_lines(self, "ignore.txt")

    def include_patterns(self) -> list[str]:
        """Per-language force-include overrides — from sibling ``include.txt``."""
        return _read_sibling_lines(self, "include.txt")

    def fixture_markers(self) -> tuple[str, ...]:
        """Filename infixes marking a **route-only** fixture — a file that is parsed for
        structure but must never be treated as a route source (e.g. Storybook stories,
        which render components in throwaway routers). Layered like ``ignore_patterns``:
        the global set here, extended per language/framework via ``super()``. Empty of
        anything language-specific at the base; TypeScript adds ``.stories.`` etc."""
        return _GLOBAL_FIXTURE_MARKERS

    def is_fixture_file(self, path: str) -> bool:
        """Whether ``path`` is a route-only fixture — either a basename fixture marker
        (``.stories.``/``.mock.``…) or a file living under a fixture directory (``mock``/
        ``__mocks__``/``fixtures``…). Structural capture is unaffected; only route/entry-point
        emitters consult this."""
        parts = path.replace("\\", "/").split("/")
        if any(marker in parts[-1] for marker in self.fixture_markers()):
            return True
        return any(seg in _GLOBAL_FIXTURE_DIRS for seg in parts[:-1])

    def parse_file(self, ctx: ParseContext) -> FileRecord:  # pragma: no cover - abstract
        raise NotImplementedError
