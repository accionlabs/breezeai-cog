"""Repository scanner.

Walks a local directory and yields ``(path, language)`` for each file that survives
the ordered filter chain — **paths only, no content reads**:

    extension allow-list (classify)  ->  ignore/include  ->  max_file_size  ->  symlink guard

Directories are pruned when ignored (so ``node_modules/`` etc. are never descended).
Symlinked directories are never followed and visited real paths are tracked, so the
walk always terminates. The ignore/include match is hierarchical: each directory's
``.repoignore`` / ``.gitignore`` / ``.repoinclude`` apply to its subtree, matched
relative to that directory.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from .ignore import IgnoreEngine, read_dir_ignore_spec, read_dir_include_spec

# (base_dir_relpath, compiled spec)
_Stack = list[tuple[str, object]]

#: Returns the language name for a path, or ``None`` if no parser claims it.
Classifier = Callable[[str], str | None]


@dataclass(frozen=True, slots=True)
class ScanEntry:
    path: str  # repo-relative, POSIX
    language: str


def _rel_to_base(rel_path: str, base: str) -> str:
    if not base:
        return rel_path
    return rel_path[len(base):].lstrip("/")


def _matches_stack(rel_path: str, stack: _Stack) -> bool:
    return any(spec.match_file(_rel_to_base(rel_path, base)) for base, spec in stack)


def scan(
    repo_root: str | Path,
    classify: Classifier,
    *,
    engine: IgnoreEngine,
    max_file_size: int,
    follow_symlinks: bool = False,
    on_skip: Callable[[str, str], None] | None = None,
) -> Iterator[ScanEntry]:
    root = Path(repo_root)
    visited: set[str] = set()

    def keep(rel: str, is_dir: bool, ig: _Stack, inc: _Stack) -> bool:
        test = rel + "/" if is_dir else rel
        ignored = engine.is_ignored_global(test) or _matches_stack(test, ig)
        if not ignored:
            return True
        return engine.is_included_global(test) or _matches_stack(test, inc)

    def walk(dir_abs: Path, dir_rel: str, ig: _Stack, inc: _Stack) -> Iterator[ScanEntry]:
        real = os.path.realpath(dir_abs)
        if real in visited:
            return
        visited.add(real)

        di = read_dir_ignore_spec(dir_abs)
        dinc = read_dir_include_spec(dir_abs)
        if di is not None:
            ig = [*ig, (dir_rel, di)]
        if dinc is not None:
            inc = [*inc, (dir_rel, dinc)]

        try:
            entries = sorted(os.scandir(dir_abs), key=lambda e: e.name)
        except OSError:
            return

        for entry in entries:
            rel = f"{dir_rel}/{entry.name}" if dir_rel else entry.name
            if entry.is_dir(follow_symlinks=follow_symlinks):
                # symlinked dirs are skipped unless follow_symlinks; the visited-set
                # guards against loops when following is enabled.
                if not keep(rel, True, ig, inc):
                    continue
                yield from walk(Path(entry.path), rel, ig, inc)
            elif entry.is_file(follow_symlinks=True):
                if not keep(rel, False, ig, inc):
                    if on_skip is not None:
                        on_skip(rel, "ignored")
                    continue
                language = classify(rel)
                if language is None:
                    if on_skip is not None:
                        on_skip(rel, "unsupported")  # no parser for this extension
                    continue
                try:
                    size = entry.stat(follow_symlinks=True).st_size
                except OSError:
                    continue
                if max_file_size and size > max_file_size:
                    if on_skip is not None:
                        on_skip(rel, "oversized")
                    continue
                yield ScanEntry(path=rel, language=language)

    yield from walk(root, "", [], [])
