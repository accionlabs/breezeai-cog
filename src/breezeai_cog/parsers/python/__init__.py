"""Python language parser — self-registers on import (discovered by
``core.registry.discover_builtin``)."""

from __future__ import annotations

from ...core.registry import register
from .parser import PythonParser

register(PythonParser())
