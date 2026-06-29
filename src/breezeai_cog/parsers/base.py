"""The parser plugin contract (ARCHITECTURE.md §4).

A ``LanguageParser`` declares capability metadata (for discovery + the
schema-version composition gate) and a ``parse_file`` that turns one file into a
:class:`~breezeai_cog.schemas.FileRecord`. ``build_index`` is an **optional**
repo-level pre-pass (Java FQCN, TS aliases, Angular mounts) — parsers that need no
cross-file context simply omit it; the pipeline calls it via ``getattr``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Sequence, runtime_checkable

from ..schemas import SCHEMA_VERSION, FileRecord


@dataclass(frozen=True, slots=True)
class ParseContext:
    """Everything a parser needs for one file (built per file by the pipeline)."""

    path: str  # repo-relative
    abs_path: Path
    source: bytes  # read by the worker (cached per-process); only the path crosses the boundary
    repo_root: Path
    capture_statements: bool = False
    text_truncation_limit: int = 1000
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

    def build_index(self, files: Sequence[Path]) -> Any | None:  # optional pre-pass
        return None

    def parse_file(self, ctx: ParseContext) -> FileRecord:  # pragma: no cover - abstract
        raise NotImplementedError
