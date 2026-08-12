"""Pure, stateless helpers — no domain logic."""

from __future__ import annotations

from .loc import count_loc
from .paths import cog_dir, repo_relative
from .source_cache import SourceCache
from .text import snippet, truncate

__all__ = ["cog_dir", "count_loc", "repo_relative", "SourceCache", "snippet", "truncate"]
