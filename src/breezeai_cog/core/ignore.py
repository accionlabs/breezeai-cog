"""Ignore / include engine.

Two pattern sets matched with ``pathspec`` (gitwildmatch):

* **universal ignore** — built-in defaults only (``default_ignores.txt``); matched at
  walk time to prune directories, plus the repo-tree ``.repoignore`` / ``.gitignore``
  files (hierarchical — the scanner accumulates a per-directory stack and matches
  relative to each file's directory).
* **per-language ignore/include** — each parser's ``ignore.txt`` / ``include.txt``
  (layer 2), kept **scoped to that language** and applied **post-scan** to a file
  only when the file's own classified language owns the rule.
* **include** — overrides the ignore set: per-language ``include.txt`` (scoped) and
  repo-tree ``.repoinclude`` (hierarchical).

A path is kept when ``included or not ignored``.

Per-language scoping: layer-2 patterns are *not* unioned into the global spec —
that leaks one language's directory-ignore into another's tree (e.g. C#/NuGet
``packages/`` pruning a pnpm ``packages/`` workspace). Instead they are compiled per
language and matched against a file only when it is a file of that language, after the
walk has classified it. Directory pruning during the walk therefore uses only the
universal built-ins (which already cover ``bin/``/``obj/``/``target/``/``dist/`` etc.),
so the language-agnostic walk can't over-prune.
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
    """Holds the *universal* (built-in) ignore/include specs used for walk-time pruning,
    plus **per-language** layer-2 specs applied post-scan. The scanner supplies the
    per-directory repo-tree stacks at match time."""

    def __init__(
        self,
        ignore_lines: Iterable[str],
        include_lines: Iterable[str],
        lang_specs: dict[str, tuple[GitIgnoreSpec, GitIgnoreSpec]] | None = None,
    ) -> None:
        self._ignore = compile_spec(ignore_lines)
        self._include = compile_spec(include_lines)
        # language name -> (ignore spec, include spec); scoped, matched post-scan.
        self._lang: dict[str, tuple[GitIgnoreSpec, GitIgnoreSpec]] = dict(lang_specs or {})

    @classmethod
    def build(cls, parsers: Iterable[object] = ()) -> "IgnoreEngine":
        # Universal built-ins prune the walk; each parser's layer-2 ignore/include is
        # compiled per language (keyed by parser name = the file's classified language)
        # and applied post-scan, so one language's rule can't prune another's tree.
        lang: dict[str, tuple[GitIgnoreSpec, GitIgnoreSpec]] = {}
        for parser in parsers:
            ig = list(getattr(parser, "ignore_patterns", list)())
            inc = list(getattr(parser, "include_patterns", list)())
            if ig or inc:
                lang[getattr(parser, "name", "")] = (compile_spec(ig), compile_spec(inc))
        return cls(builtin_ignore_lines(), [], lang)

    def is_ignored_global(self, rel: str) -> bool:
        return self._ignore.match_file(rel)

    def is_included_global(self, rel: str) -> bool:
        return self._include.match_file(rel)

    def is_lang_ignored(self, rel: str, language: str) -> bool:
        """Whether ``rel`` matches its own ``language``'s layer-2 ignore patterns."""
        spec = self._lang.get(language)
        return spec is not None and spec[0].match_file(rel)

    def is_lang_included(self, rel: str, language: str) -> bool:
        """Whether ``rel`` is force-included by its own ``language``'s layer-2 patterns."""
        spec = self._lang.get(language)
        return spec is not None and spec[1].match_file(rel)
