"""Lines-of-code counting."""

from __future__ import annotations


def count_loc(text: str) -> int:
    """Non-blank physical line count (blank / whitespace-only lines excluded)."""
    return sum(1 for line in text.splitlines() if line.strip())
