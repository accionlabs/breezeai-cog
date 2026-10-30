"""Ruby language parser package scaffold.

This placeholder matches the repository's parser-package convention without modifying
any existing parser registration or core logic.

``ownership`` is a shared helper used by all Ruby framework parsers to assign
``parentId`` on synthesised route statements; it is intentionally kept separate
from the base parser so framework parsers can import it without pulling in
all of ``parser.py``.
"""

from __future__ import annotations

from .ownership import owner_id  # re-export for framework-parser use
from .parser import RubyParser

PARSERS = [RubyParser()]

__all__ = ["RubyParser", "PARSERS", "owner_id"]
