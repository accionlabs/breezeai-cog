"""The parser plugin contract (ARCHITECTURE.md §4).

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
from typing import Any, Protocol, Sequence, runtime_checkable

from ..schemas import SCHEMA_VERSION, FileRecord


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


@dataclass(frozen=True, slots=True)
class ParseContext:
    """Everything a parser needs for one file (built per file by the pipeline)."""

    path: str  # repo-relative
    abs_path: Path
    source: bytes  # read by the worker (cached per-process); only the path crosses the boundary
    repo_root: Path
    capture_statements: bool = False
    text_truncation_limit: int = 8000
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

    def matches(self, path: str | Path) -> bool:
        """Whether this parser handles ``path`` by name/extension (the candidacy gate,
        before ``claims``). Default: suffix or exact filename in ``extensions``. Override
        for filename patterns (e.g. the config parser's ``Dockerfile.*`` / ``.env.*``)."""
        p = Path(path)
        return p.suffix in self.extensions or p.name in self.extensions

    def claims(self, path: str, source: bytes) -> bool:
        """Whether this parser should handle ``path``. The base language parser claims
        everything of its extension (fallback); framework parsers override this to sniff
        their framework's signature in ``source`` (e.g. ``b"@nestjs/" in source``)."""
        return True

    def build_index(self, repo_root: Path, files: Sequence[Path]) -> Any | None:  # optional pre-pass
        return None

    def ignore_patterns(self) -> list[str]:
        """Per-language ignore defaults (layer 2, §9) — from sibling ``ignore.txt``."""
        return _read_sibling_lines(self, "ignore.txt")

    def include_patterns(self) -> list[str]:
        """Per-language force-include overrides (§9) — from sibling ``include.txt``."""
        return _read_sibling_lines(self, "include.txt")

    def fixture_markers(self) -> tuple[str, ...]:
        """Filename infixes marking a **route-only** fixture — a file that is parsed for
        structure but must never be treated as a route source (e.g. Storybook stories,
        which render components in throwaway routers). Layered like ``ignore_patterns``:
        the global set here, extended per language/framework via ``super()``. Empty of
        anything language-specific at the base; TypeScript adds ``.stories.`` etc."""
        return _GLOBAL_FIXTURE_MARKERS

    def is_fixture_file(self, path: str) -> bool:
        """Whether ``path`` is a route-only fixture for this parser's language/framework."""
        base = path.rsplit("/", 1)[-1]
        return any(marker in base for marker in self.fixture_markers())

    def parse_file(self, ctx: ParseContext) -> FileRecord:  # pragma: no cover - abstract
        raise NotImplementedError
