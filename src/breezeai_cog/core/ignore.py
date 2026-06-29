"""Ignore / include engine (ARCHITECTURE.md §9).

Two pattern sets matched with ``pathspec`` (gitwildmatch):

* **ignore** — additive union, no cross-layer "winning": built-in defaults +
  per-language ``ignore.txt`` (the *global* part, here) and the repo-tree
  ``.repoignore`` / ``.gitignore`` files (hierarchical — the scanner accumulates a
  per-directory stack and matches relative to each file's directory).
* **include** — overrides the ignore set: per-language ``include.txt`` (global) and
  repo-tree ``.repoinclude`` (hierarchical).

A path is kept when ``included or not ignored``.

Simplification vs §9: the *global* part unions built-in + **all** registered
parsers' ``ignore.txt``/``include.txt`` rather than scoping per detected language.
Per-language artifact patterns are language-specific names, so the union is
functionally equivalent except for rare cross-language path collisions (a strict
per-language scoping is a later refinement). This keeps directory pruning — where
the language is unknown — simple and correct.
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path
from typing import Iterable

from pathspec import GitIgnoreSpec

_REPO_IGNORE_FILES = (".repoignore", ".gitignore")
_REPO_INCLUDE_FILE = ".repoinclude"


def compile_spec(lines: Iterable[str]) -> GitIgnoreSpec:
    """Compile gitignore patterns (``pathspec`` ignores blanks and ``#`` lines)."""
    return GitIgnoreSpec.from_lines(list(lines))


def builtin_ignore_lines() -> list[str]:
    return files("breezeai_cog.core").joinpath("default_ignores.txt").read_text("utf-8").splitlines()


def _read_lines(path: Path) -> list[str]:
    try:
        return path.read_text("utf-8", errors="replace").splitlines()
    except OSError:
        return []


def read_dir_ignore_spec(dir_path: str | Path) -> GitIgnoreSpec | None:
    """Combined ``.repoignore`` + ``.gitignore`` spec for one directory, or ``None``."""
    lines: list[str] = []
    for name in _REPO_IGNORE_FILES:
        p = Path(dir_path) / name
        if p.is_file():
            lines += _read_lines(p)
    return compile_spec(lines) if lines else None


def read_dir_include_spec(dir_path: str | Path) -> GitIgnoreSpec | None:
    """``.repoinclude`` spec for one directory, or ``None``."""
    p = Path(dir_path) / _REPO_INCLUDE_FILE
    return compile_spec(_read_lines(p)) if p.is_file() else None


class IgnoreEngine:
    """Holds the *global* ignore/include specs; the scanner supplies the per-directory
    repo-tree stacks at match time."""

    def __init__(self, ignore_lines: Iterable[str], include_lines: Iterable[str]) -> None:
        self._ignore = compile_spec(ignore_lines)
        self._include = compile_spec(include_lines)

    @classmethod
    def build(cls, parsers: Iterable[object] = ()) -> "IgnoreEngine":
        ignore = builtin_ignore_lines()
        include: list[str] = []
        for parser in parsers:
            ignore += getattr(parser, "ignore_patterns", list)()
            include += getattr(parser, "include_patterns", list)()
        return cls(ignore, include)

    def is_ignored_global(self, rel: str) -> bool:
        return self._ignore.match_file(rel)

    def is_included_global(self, rel: str) -> bool:
        return self._include.match_file(rel)
