"""Error taxonomy (see ARCHITECTURE.md §11).

Two broad classes of failure:
* **fatal** (``ConfigError``, ``RegistryError``) — abort the run, fail fast.
* **per-file** (``ParseError``, ``ParserTimeout``) — caught by the pipeline, logged
  with context, the file is dropped and counted; the repo always terminates.

``UploadError`` covers outbound upload/network failures (bounded retry; backend
notifications stay fire-and-forget).
"""

from __future__ import annotations


class BreezeCogError(Exception):
    """Base class for all breezeai-cog errors."""


class ConfigError(BreezeCogError):
    """Invalid configuration — fatal, fail fast (§8)."""


class RegistryError(BreezeCogError):
    """Parser registration / lookup problem — fatal."""


class ParseError(BreezeCogError):
    """A single file failed to parse/extract — caught per file, file dropped (§5)."""

    def __init__(self, message: str, *, path: str | None = None, parser: str | None = None) -> None:
        super().__init__(message)
        self.path = path
        self.parser = parser


class ParserTimeout(ParseError):
    """A file exceeded the tree-sitter parse timeout — treated like ``ParseError``."""


class UploadError(BreezeCogError):
    """Outbound upload / network failure (bounded retry; §10/§11)."""
