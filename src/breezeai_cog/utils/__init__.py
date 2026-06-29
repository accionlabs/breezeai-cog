"""Pure, stateless helpers — no domain logic (ARCHITECTURE.md §2)."""

from __future__ import annotations

from .loc import count_loc
from .paths import repo_relative
from .source_cache import SourceCache
from .text import snippet, truncate

__all__ = ["count_loc", "repo_relative", "SourceCache", "snippet", "truncate"]
