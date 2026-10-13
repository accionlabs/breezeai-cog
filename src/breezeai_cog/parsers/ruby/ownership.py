"""Ownership helpers for synthesized Ruby statements."""

from __future__ import annotations

from ...schemas import Class, Function


def owner_id(line: int, functions: list[Function], classes: list[Class], fallback: str) -> str:
    """Return the most-specific function/class spanning ``line``, or ``fallback``."""
    matching_functions = [f for f in functions if f.startLine <= line <= f.endLine]
    if matching_functions:
        return min(matching_functions, key=lambda f: f.endLine - f.startLine).id

    matching_classes = [c for c in classes if c.startLine <= line <= c.endLine]
    if matching_classes:
        return min(matching_classes, key=lambda c: c.endLine - c.startLine).id
    return fallback