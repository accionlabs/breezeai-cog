"""Source-text helpers: snippet slicing and truncation."""

from __future__ import annotations


def truncate(text: str, limit: int) -> str:
    """Cap ``text`` at ``limit`` characters (``limit <= 0`` disables truncation)."""
    if limit and limit > 0 and len(text) > limit:
        return text[:limit]
    return text


def snippet(source: str, start_line: int, end_line: int) -> str:
    """Return the 1-based, inclusive ``[start_line, end_line]`` line range of ``source``."""
    lines = source.splitlines()
    return "\n".join(lines[max(start_line - 1, 0):end_line])
