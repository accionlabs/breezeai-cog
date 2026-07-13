"""The capture ``id``/``parentId`` convention — defined once,
used by parsers when building records. The position suffix keeps anonymous
functions, overloads, and same-line statements from colliding.

    File      -> path
    Class     -> path#ClassName            (nested: path#Outer.Inner)
    Function  -> path#[Class#]name@startLine   (name='<anonymous>' if unnamed)
    Statement -> path:startLine:startCol

The id is capture-only: the backend consumes it to wire containment edges, then
discards it. It only needs to be unique within a file — :func:`disambiguate` adds a
deterministic ordinal on the rare residual clash.
"""

from __future__ import annotations

ANONYMOUS = "<anonymous>"


def file_id(path: str) -> str:
    return path


def class_id(path: str, name: str) -> str:
    return f"{path}#{name}"


def function_id(path: str, name: str | None, start_line: int, *, class_name: str | None = None) -> str:
    owner = f"{class_name}#" if class_name else ""
    return f"{path}#{owner}{name or ANONYMOUS}@{start_line}"


def statement_id(path: str, start_line: int, start_col: int) -> str:
    return f"{path}:{start_line}:{start_col}"


def disambiguate(candidate: str, seen: set[str]) -> str:
    """Return a unique id, appending ``#2``, ``#3``, … if ``candidate`` is taken.
    Records the result in ``seen``."""
    if candidate not in seen:
        seen.add(candidate)
        return candidate
    i = 2
    while f"{candidate}#{i}" in seen:
        i += 1
    unique = f"{candidate}#{i}"
    seen.add(unique)
    return unique
